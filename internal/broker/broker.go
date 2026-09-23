// Package broker implements the in-memory MQTT 3.1.1 subset broker:
// sessions, topic subscriptions, QoS 1 delivery with per-session packet
// identifiers, DUP retransmission on reconnect, and durable sessions.
//
// Delivery guarantee: AT LEAST ONCE. QoS 1 messages are tracked per
// receiving session until that session returns PUBACK; on reconnect (or
// retry timeout) they are redelivered with DUP=1. The broker never claims
// exactly-once: redelivery duplicates are possible by design whenever a
// PUBACK is lost, and application-level de-duplication is the consumer's
// responsibility.
package broker

import (
	"errors"
	"net"
	"sync"
	"time"

	"mqttsub/internal/mqtt"
)

// Message is a PUBLISH in flight inside the broker.
type Message struct {
	Topic   string
	QoS     byte
	Payload []byte

	// PacketID is assigned by the receiving session for outbound QoS 1.
	PacketID uint16
}

// Session is one client's MQTT session. When durable (clean=false) it
// outlives its network connection and is persisted to disk.
//
// All fields are guarded by Broker.mu. The conn back-reference is nil
// while the session is offline.
type Session struct {
	ClientID string
	Durable  bool

	// subs maps topic filter -> maximum granted QoS (0 or 1).
	subs map[string]byte

	// pending is the FIFO queue not yet sent on the current (or any)
	// connection: QoS 0/1 messages waiting for delivery.
	pending []*Message
	// inflight holds QoS 1 messages already sent (or being redelivered)
	// but not yet acknowledged, keyed for ordering in inflightOrder.
	inflight map[uint16]*Message
	// inflightOrder is the send order of inflight messages; it is the
	// redelivery order on reconnect (oldest unacknowledged first).
	inflightOrder []uint16

	// seenInbound tracks inbound QoS 1 packet identifiers the server has
	// answered with PUBACK during this session. It lets a redelivered
	// DUP=1 PUBLISH be answered with a repeat PUBACK without being
	// re-distributed to subscribers.
	seenInbound map[uint16]bool

	// nextPID is the next candidate packet id for outbound traffic.
	nextPID uint16

	conn *Conn

	// dirty marks state the debounced persistence loop must flush.
	dirty bool
}

func newSession(id string, durable bool) *Session {
	return &Session{
		ClientID:    id,
		Durable:     durable,
		subs:        map[string]byte{},
		inflight:    map[uint16]*Message{},
		seenInbound: map[uint16]bool{},
		nextPID:     1,
	}
}

// Stats are simple counters surfaced through the HTTP API.
type Stats struct {
	PublishedQoS0 int64 `json:"published_qos0"`
	PublishedQoS1 int64 `json:"published_qos1"`
	DeliveredQoS0 int64 `json:"delivered_qos0"`
	DeliveredQoS1 int64 `json:"delivered_qos1"`
	Inflight      int   `json:"inflight"`
	Sessions      int   `json:"sessions"`
	Online        int   `json:"online"`
}

// Config configures a Broker.
type Config struct {
	// StorePath is the JSON file durable sessions are snapshotted to.
	// Empty disables persistence entirely.
	StorePath string
	// RetryInterval is the live (connected) QoS1 redelivery timer.
	// Zero disables live retry; redelivery then happens on reconnect.
	RetryInterval time.Duration
}

// Broker is the server. Its methods are safe for concurrent use.
type Broker struct {
	mu       sync.Mutex
	sessions map[string]*Session
	stats    Stats
	store    *Store
	retry    time.Duration
	wg       sync.WaitGroup

	stopCh chan struct{}
	closed bool

	// wakeCh is buffered(1); the persistence loop wakes when dirty.
	wakeCh chan struct{}
}

// ErrClosed is returned by operations after Shutdown.
var ErrClosed = errors.New("broker: closed")

// New creates a Broker and loads durable sessions from disk.
func New(cfg Config) (*Broker, error) {
	b := &Broker{
		sessions: map[string]*Session{},
		store:    NewStore(cfg.StorePath),
		retry:    cfg.RetryInterval,
		stopCh:   make(chan struct{}),
		wakeCh:   make(chan struct{}, 1),
	}
	sf, err := b.store.load()
	if err != nil {
		return nil, err
	}
	for i := range sf.Sessions {
		snap := sf.Sessions[i]
		s := newSession(snap.ClientID, true)
		s.nextPID = snap.NextPID
		if s.nextPID == 0 {
			s.nextPID = 1
		}
		for f, q := range snap.Subs {
			s.subs[f] = q
		}
		for _, pid := range snap.SeenInbound {
			s.seenInbound[pid] = true
		}
		for _, m := range snap.Pending {
			s.pending = append(s.pending, &Message{
				Topic: m.Topic, QoS: m.QoS, Payload: append([]byte(nil), m.Payload...),
			})
		}
		for _, m := range snap.Inflight {
			if m.PacketID == 0 {
				continue
			}
			s.inflight[m.PacketID] = &Message{
				Topic: m.Topic, QoS: m.QoS, Payload: append([]byte(nil), m.Payload...),
				PacketID: m.PacketID,
			}
			s.inflightOrder = append(s.inflightOrder, m.PacketID)
		}
		b.sessions[s.ClientID] = s
	}
	b.wg.Add(1)
	go b.persistLoop()
	return b, nil
}

// Serve runs the MQTT protocol on one accepted connection. It blocks until
// the connection ends (clean DISCONNECT, protocol violation, or I/O error).
func (b *Broker) Serve(nc net.Conn) {
	c := newConn(b, nc)
	c.serve()
}

// Publish injects a server-originated PUBLISH (used by the HTTP API and for
// will messages). qos must be 0 or 1. It returns the number of matching
// sessions the message was handed to (online or durable offline queue).
func (b *Broker) Publish(topic string, qos byte, payload []byte) (matched int, err error) {
	if qos > 1 {
		return 0, errors.New("broker: only QoS 0 and 1 are supported")
	}
	if !mqtt.ValidTopic(topic) {
		return 0, errors.New("broker: invalid topic")
	}
	b.mu.Lock()
	if qos == 0 {
		b.stats.PublishedQoS0++
	} else {
		b.stats.PublishedQoS1++
	}
	var targets []*Session
	for _, s := range b.sessions {
		if sessionMatches(s, topic) {
			targets = append(targets, s)
		}
	}
	for _, s := range targets {
		effective := qos
		if q := maxSubQoS(s, topic); q < effective {
			effective = q
		}
		m := &Message{Topic: topic, QoS: effective, Payload: append([]byte(nil), payload...)}
		if s.conn != nil {
			s.pending = append(s.pending, m)
			matched++
			if effective == 0 {
				b.stats.DeliveredQoS0++
			} else {
				b.stats.DeliveredQoS1++
			}
		} else if s.Durable {
			// Offline durable session: QoS 1 (and QoS 0) is queued
			// per MQTT-3.1.2-5/§4.1 until the client returns.
			s.pending = append(s.pending, m)
			s.dirty = true
			matched++
		}
	}
	b.mu.Unlock()

	// Wake pumps outside the lock; only online sessions were appended.
	b.mu.Lock()
	for _, s := range targets {
		if s.conn != nil {
			s.conn.kick()
		}
	}
	if matched > 0 {
		b.markDirtyLocked()
	}
	b.mu.Unlock()
	return matched, nil
}

func sessionMatches(s *Session, topic string) bool {
	for f := range s.subs {
		if mqtt.TopicMatch(f, topic) {
			return true
		}
	}
	return false
}

// maxSubQoS returns the highest granted QoS among the session's filters
// matching topic. Caller holds broker mu.
func maxSubQoS(s *Session, topic string) byte {
	var best byte = 0
	found := false
	for f, q := range s.subs {
		if mqtt.TopicMatch(f, topic) {
			if !found || q > best {
				best = q
				found = true
			}
		}
	}
	return best
}

// markDirtyLocked signals the persistence loop. Caller holds mu.
func (b *Broker) markDirtyLocked() {
	select {
	case b.wakeCh <- struct{}{}:
	default:
	}
}

// Shutdown stops persistence and flushes the final snapshot. Active
// network connections are not force-closed here; callers close the
// listener and may close conns separately.
func (b *Broker) Shutdown() error {
	b.mu.Lock()
	if b.closed {
		b.mu.Unlock()
		return nil
	}
	b.closed = true
	close(b.stopCh)
	b.mu.Unlock()
	b.wg.Wait()
	return b.flush()
}
