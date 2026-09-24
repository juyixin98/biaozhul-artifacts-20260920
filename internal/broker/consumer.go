// Package broker wires the MQTT v5 client to the transactional store.
//
// Delivery/ACK contract
//
//	The paho v5 client runs with EnableManualAcknowledgment. A QoS 1 PUBLISH
//	gets PUBACK only after the Postgres transaction for THAT message has
//	committed. Until then the broker keeps the message inflight and redelivers
//	it (DUP=1) after a forced reconnect or a consumer restart (durable session,
//	fixed client id, CleanStart=false, SessionExpiryInterval > 0).
//
// Fairness
//
//	Messages are fanned out to one serial worker per device_id. A device whose
//	valid messages keep failing (fault injection) stalls only its own queue
//	plus the connection cycle; every other device keeps committing. Invalid
//	payloads are quarantined in-line and ACKed immediately, so a misbehaving
//	device sending garbage cannot block anyone either.
package broker

import (
	"context"
	"fmt"
	"log"
	"net"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	pahomqtt "github.com/eclipse/paho.golang/paho"

	"github.com/jackc/pgx/v5"

	"mqttredel/internal/crypto"
	"mqttredel/internal/store"
	"mqttredel/internal/telemetry"
)

// pgxTx is the transaction handle type expected by store.ProcessEvent.
type pgxTx = pgx.Tx

// outcome of processing one delivery.
type outcome int

const (
	outcomeAck       outcome = iota // durably handled; send PUBACK
	outcomeRedeliver                // valid msg, tx rolled back; reconnect to trigger redelivery
	outcomeDropAck                  // tx committed but ACK must be lost (one-shot fault)
)

// Config for the consumer.
type Config struct {
	Server   string // host:port, e.g. "127.0.0.1:11883"
	ClientID string // fixed client id => durable session across restarts
	Topic    string // e.g. "telemetry/#"
}

// Stats are processing counters since process start.
type Stats struct {
	Received    int64 `json:"received"`
	Inserted    int64 `json:"inserted"`
	Duplicates  int64 `json:"duplicates"`
	Snapshots   int64 `json:"snapshots"`
	Quarantined int64 `json:"quarantined"`
	TxFailures  int64 `json:"tx_failures"`
	AckDropped  int64 `json:"ack_dropped"`
	Reconnects  int64 `json:"reconnects"`
}

// Consumer is a running MQTT subscriber.
type Consumer struct {
	cfg Config
	log *log.Logger

	faults *Faults

	// cur holds the active connection state. It is swapped atomically on
	// every (re)connect; callbacks from an old generation must ignore a loss
	// that a newer generation has already superseded.
	cur  atomic.Pointer[connState]
	gen  atomic.Uint64
	lost chan uint64 // receives the generation whose connection died

	queuesMu sync.Mutex
	queues   map[string]chan *job

	closed chan struct{}
	wg     sync.WaitGroup

	received, inserted, duplicates, snapshots, quarantined atomic.Int64
	txFailures, ackDropped, reconnects                     atomic.Int64
}

// connState is the state of one physical MQTT connection. Each (re)connect
// creates a fresh one, so an ACK is always sent on the tracker/client of the
// exact connection that delivered the message.
type connState struct {
	gen    uint64
	conn   net.Conn
	client *pahomqtt.Client
	done   chan struct{} // closed once, when this connection is known dead
	once   sync.Once
}

// markDead closes this connection's done channel exactly once and notifies the
// supervisor — but only if this generation is still the current one, so stale
// callbacks from a replaced client cannot trigger a spurious reconnect.
func (cs *connState) markDead(c *Consumer, reason string) {
	cs.once.Do(func() {
		close(cs.done)
		if c.cur.Load() == cs {
			c.log.Printf("connection gen=%d dead (%s)", cs.gen, reason)
			select {
			case c.lost <- cs.gen:
			case <-c.closed:
			}
		}
	})
}

// alive reports whether this connection is still the active, undead one.
func (cs *connState) alive() bool {
	select {
	case <-cs.done:
		return false
	default:
		return true
	}
}

type job struct {
	pb    *pahomqtt.Publish
	state *connState // connection on which this delivery arrived
}

// NewConsumer constructs (but does not connect) a consumer.
func NewConsumer(cfg Config, logger *log.Logger, faults *Faults) *Consumer {
	if faults == nil {
		faults = NewFaults()
	}
	return &Consumer{
		cfg:    cfg,
		log:    logger,
		faults: faults,
		queues: make(map[string]chan *job),
		lost:   make(chan uint64, 8),
		closed: make(chan struct{}),
	}
}

// Stats returns a snapshot of processing counters.
func (c *Consumer) Stats() Stats {
	return Stats{
		Received:    c.received.Load(),
		Inserted:    c.inserted.Load(),
		Duplicates:  c.duplicates.Load(),
		Snapshots:   c.snapshots.Load(),
		Quarantined: c.quarantined.Load(),
		TxFailures:  c.txFailures.Load(),
		AckDropped:  c.ackDropped.Load(),
		Reconnects:  c.reconnects.Load(),
	}
}

// IsConnected reports MQTT connection state.
func (c *Consumer) IsConnected() bool {
	cs := c.cur.Load()
	return cs != nil && cs.alive()
}

// Run connects and supervises the connection until ctx is cancelled.
// On a dropped connection it reconnects with the same durable session; unacked
// QoS1 messages are redelivered by the broker with DUP=1.
func (c *Consumer) Run(ctx context.Context) error {
	if err := c.connect(ctx); err != nil {
		return err
	}
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-c.closed:
			return nil
		case gen := <-c.lost:
			if c.cur.Load() == nil || c.cur.Load().gen != gen {
				continue // a newer generation already replaced it
			}
			c.reconnects.Add(1)
			c.log.Printf("connection lost; waiting for business failure to clear before reconnect")
			// Back off until the injected failure clears. Reconnecting with the
			// fault still armed would just immediately fail the redelivery again.
			delay := time.Second
			timer := time.NewTimer(delay)
			for c.anyFailureActive() {
				c.log.Printf("business failure still armed; backoff %s", delay)
				timer.Reset(delay)
				select {
				case <-ctx.Done():
					timer.Stop()
					return ctx.Err()
				case <-c.closed:
					timer.Stop()
					return nil
				case <-timer.C:
				}
				if delay < 8*time.Second {
					delay *= 2
				}
			}
			timer.Stop()
			if err := c.connect(ctx); err != nil {
				return fmt.Errorf("reconnect: %w", err)
			}
			c.log.Printf("reconnected with durable session; broker is redelivering unacked QoS1 messages")
		}
	}
}

// anyFailureActive gates reconnect: if a commit-failure is still armed we
// back off rather than instantly failing the redelivery again.
func (c *Consumer) anyFailureActive() bool {
	return c.faults.FailHook("*")
}

func (c *Consumer) connect(ctx context.Context) error {
	dialCtx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	conn, err := (&net.Dialer{}).DialContext(dialCtx, "tcp", c.cfg.Server)
	if err != nil {
		return fmt.Errorf("dial mqtt %s: %w", c.cfg.Server, err)
	}

	gen := c.gen.Add(1)
	cs := &connState{gen: gen, conn: conn, done: make(chan struct{})}

	client := pahomqtt.NewClient(pahomqtt.ClientConfig{
		Conn: conn,
		OnPublishReceived: []func(pahomqtt.PublishReceived) (bool, error){
			func(pr pahomqtt.PublishReceived) (bool, error) {
				c.dispatch(cs, pr.Packet)
				return false, nil
			},
		},
		OnClientError: func(e error) {
			c.log.Printf("mqtt client error (gen=%d): %v", gen, e)
			cs.markDead(c, "client error")
		},
		OnServerDisconnect: func(d *pahomqtt.Disconnect) {
			c.log.Printf("server disconnected gen=%d: code=%d", gen, d.ReasonCode)
			cs.markDead(c, "server disconnect")
		},
		EnableManualAcknowledgment: true,
	})
	cs.client = client

	ca, err := client.Connect(ctx, &pahomqtt.Connect{
		// ClientID must live IN the packet (ClientConfig.ClientID alone is not
		// copied into the wire CONNECT). An empty ClientID forces CleanStart
		// per MQTT5, which would silently destroy durability.
		ClientID:   c.cfg.ClientID,
		KeepAlive:  30,
		CleanStart: false, // durable session
		Properties: &pahomqtt.ConnectProperties{
			SessionExpiryInterval: ptr(uint32(7200)), // session survives 2h without us
		},
	})
	if err != nil {
		conn.Close()
		return fmt.Errorf("mqtt connect: %w", err)
	}
	if ca.ReasonCode != 0 {
		conn.Close()
		return fmt.Errorf("mqtt connect rejected: code=%d", ca.ReasonCode)
	}

	if _, err := client.Subscribe(ctx, &pahomqtt.Subscribe{
		Subscriptions: []pahomqtt.SubscribeOptions{
			{
				Topic: c.cfg.Topic, QoS: 1,
				// RetainHandling 1 = send retained messages only when the
				// subscription is FIRST established, NOT on every reconnect.
				// Without this, each redelivery cycle replayed the snapshot.
				RetainHandling: 1,
			},
		},
	}); err != nil {
		conn.Close()
		return fmt.Errorf("subscribe: %w", err)
	}

	c.cur.Store(cs)
	c.log.Printf("connected gen=%d to %s, subscribed to %s (sessionPresent=%v)",
		gen, c.cfg.Server, c.cfg.Topic, ca.SessionPresent)
	return nil
}

// dispatch routes one inbound PUBLISH to the per-device serial worker.
// Routing never blocks on database work.
func (c *Consumer) dispatch(cs *connState, pb *pahomqtt.Publish) {
	c.received.Add(1)
	key := deviceKey(pb.Topic)
	c.queuesMu.Lock()
	q, ok := c.queues[key]
	if !ok {
		q = make(chan *job, 1024)
		c.queues[key] = q
		c.wg.Add(1)
		go c.worker(key, q)
	}
	c.queuesMu.Unlock()
	select {
	case q <- &job{pb: pb, state: cs}:
	case <-c.closed:
	}
}

func deviceKey(topic string) string {
	if strings.HasPrefix(topic, telemetry.TopicPrefix) {
		dev := strings.TrimPrefix(topic, telemetry.TopicPrefix)
		if i := strings.IndexByte(dev, '/'); i >= 0 {
			dev = dev[:i]
		}
		if dev != "" && !strings.ContainsAny(dev, "+#") {
			return "dev:" + dev
		}
	}
	// Garbage topics share one worker: a flood of bad topics cannot create
	// unbounded goroutines.
	return "invalid"
}

func (c *Consumer) worker(key string, q <-chan *job) {
	defer c.wg.Done()
	for j := range q {
		c.handleOne(j)
	}
}

// handleOne makes the full process→commit→ACK decision for one delivery.
func (c *Consumer) handleOne(j *job) {
	pb, cs := j.pb, j.state
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()

	oc, dev := c.process(ctx, pb)
	switch oc {
	case outcomeRedeliver:
		// VALID message, business tx did not commit → do NOT ACK. Drop the
		// TCP connection; the supervisor reconnects (once the fault clears)
		// with the durable session and the broker redelivers this message
		// with DUP=1. Other devices' queues keep processing meanwhile.
		c.txFailures.Add(1)
		c.log.Printf("NO-ACK device=%s topic=%s packet=%d dup=%v → forcing reconnect so broker redelivers",
			dev, pb.Topic, pb.PacketID, pb.Duplicate())
		cs.markDead(c, "business commit failure")
		_ = cs.conn.Close()
	case outcomeDropAck:
		// Transaction COMMITTED but the ACK is deliberately lost (simulating a
		// crash right after commit). Drop the connection without PUBACK: the
		// broker will redeliver, and reprocessing finds the business key
		// already present → duplicate branch, then ACK. Exactly one business
		// row despite two deliveries.
		c.ackDropped.Add(1)
		c.log.Printf("ACK-DROPPED-AFTER-COMMIT device=%s topic=%s packet=%d → redelivery must dedup",
			dev, pb.Topic, pb.PacketID)
		cs.markDead(c, "simulated ack loss")
		_ = cs.conn.Close()
	case outcomeAck:
		// All durable outcomes (event / dup / snapshot / quarantine) are ACKed
		// only after their transaction committed, on the connection that
		// delivered them.
		if !cs.alive() {
			// Connection already dropped (e.g. another device's fault on a
			// shared link). The broker will redeliver; business dedup makes
			// that safe and idempotent, so this is not an error.
			c.log.Printf("skip ack on dead connection packet=%d (redelivery will dedup)", pb.PacketID)
			break
		}
		if err := cs.client.Ack(pb); err != nil {
			c.log.Printf("ack failed (broker will redeliver, business dedup keeps it safe): %v", err)
		}
	}
}

// process classifies one delivery.
//
//	outcomeRedeliver: valid payload whose business transaction did not commit
//	                  (or storage failed on snapshot/quarantine) — no ACK.
//	outcomeDropAck:   valid payload whose transaction COMMITTED, but the
//	                  one-shot drop-ACK fault is armed — no ACK despite commit.
//	outcomeAck:       durably handled (new event, duplicate, retained snapshot
//	                  audited, or invalid payload quarantined) — ACK.
func (c *Consumer) process(ctx context.Context, pb *pahomqtt.Publish) (outcome, string) {
	topicDevice := strings.TrimPrefix(pb.Topic, telemetry.TopicPrefix)
	if i := strings.IndexByte(topicDevice, '/'); i >= 0 {
		topicDevice = topicDevice[:i]
	}

	// RETAINED delivery on subscribe = broker's stored snapshot, NOT a fresh
	// sample: audit only. Never creates an event, never touches online state.
	if pb.Retain {
		if err := store.RecordSnapshot(ctx, topicDevice, pb.Topic, pb.PacketID, pb.Payload); err != nil {
			c.log.Printf("snapshot persist failed topic=%s: %v", pb.Topic, err)
			return outcomeRedeliver, topicDevice // storage down → redeliver
		}
		c.snapshots.Add(1)
		c.log.Printf("RETAINED snapshot audited, online state untouched: topic=%s bytes=%d",
			pb.Topic, len(pb.Payload))
		return outcomeAck, ""
	}

	msg, err := telemetry.ParseAndValidate(topicDevice, pb.Payload)
	if err != nil {
		return c.quarantine(ctx, pb, topicDevice, err.Error())
	}

	secret, known, err := store.DeviceSecret(ctx, msg.DeviceID)
	if err != nil {
		c.log.Printf("device lookup error: %v", err)
		return outcomeRedeliver, msg.DeviceID
	}
	if !known {
		return c.quarantine(ctx, pb, msg.DeviceID, "unregistered device_id")
	}

	fields := crypto.CanonicalFields{
		DeviceID: msg.DeviceID, BootGen: msg.BootGen, Seq: msg.Seq,
		Value: msg.Value, TSMillis: msg.TSMillis,
	}
	if !crypto.Verify(secret, fields, msg.Sig) {
		return c.quarantine(ctx, pb, msg.DeviceID, "HMAC-SHA256 signature mismatch")
	}

	in := store.EventInput{
		DeviceID:   msg.DeviceID,
		BootGen:    msg.BootGen,
		Seq:        msg.Seq,
		Value:      msg.Value,
		MeasuredAt: time.UnixMilli(msg.TSMillis).UTC(),
		Topic:      pb.Topic,
		PacketID:   pb.PacketID,
		DupFlag:    pb.Duplicate(),
		PayloadRaw: pb.Payload,
	}
	var result store.EventResult
	err = store.InTx(ctx, func(tx pgxTx) error {
		r, e := store.ProcessEvent(ctx, tx, in, commitFailureDevice(c.faults, msg.DeviceID))
		if e != nil {
			return e
		}
		result = r
		return nil
	})
	if err != nil {
		// Any failure on a VALID message (including injected business
		// failure or commit failure) means nothing durable happened →
		// redeliver, no ACK.
		c.log.Printf("business tx rolled back device=%s boot=%d seq=%d: %v",
			msg.DeviceID, msg.BootGen, msg.Seq, err)
		return outcomeRedeliver, msg.DeviceID
	}

	switch result {
	case store.ResultInserted:
		c.inserted.Add(1)
		c.log.Printf("COMMITTED device=%s boot=%d seq=%d value=%v dup=%v packet=%d",
			msg.DeviceID, msg.BootGen, msg.Seq, msg.Value, pb.Duplicate(), pb.PacketID)
	case store.ResultDuplicate:
		c.duplicates.Add(1)
		c.log.Printf("DEDUP device=%s boot=%d seq=%d dup=%v packet=%d (one business row, two deliveries)",
			msg.DeviceID, msg.BootGen, msg.Seq, pb.Duplicate(), pb.PacketID)
	}

	// Commit succeeded. The drop-ACK fault is checked ONLY here, so it never
	// suppresses an ACK for anything that did not actually commit.
	if c.faults.ConsumeDropAck(msg.DeviceID) {
		return outcomeDropAck, msg.DeviceID
	}
	return outcomeAck, ""
}

// commitFailureDevice returns the device id when its business tx must fail.
func commitFailureDevice(f *Faults, id string) string {
	if f.FailHook(id) {
		return id
	}
	return ""
}

func (c *Consumer) quarantine(ctx context.Context, pb *pahomqtt.Publish, hint, reason string) (outcome, string) {
	if err := store.RecordQuarantine(ctx, hint, pb.Topic, pb.PacketID, pb.Duplicate(), pb.Retain, pb.Payload, reason); err != nil {
		c.log.Printf("quarantine persist failed topic=%s: %v", pb.Topic, err)
		return outcomeRedeliver, hint // only redelivered when storage itself is down
	}
	c.quarantined.Add(1)
	c.log.Printf("QUARANTINE topic=%s reason=%q bytes=%d — ACKed, other devices unaffected",
		pb.Topic, reason, len(pb.Payload))
	return outcomeAck, ""
}

// Close stops workers and disconnects gracefully.
func (c *Consumer) Close() {
	select {
	case <-c.closed:
		return
	default:
	}
	close(c.closed)
	c.queuesMu.Lock()
	for _, q := range c.queues {
		close(q)
	}
	c.queues = make(map[string]chan *job)
	c.queuesMu.Unlock()
	c.wg.Wait()

	if cs := c.cur.Load(); cs != nil {
		_ = cs.client.Disconnect(&pahomqtt.Disconnect{ReasonCode: 0})
		_ = cs.conn.Close()
	}
}
