package mqtt

import (
	"bufio"
	"errors"
	"fmt"
	"net"
	"sync"
	"sync/atomic"
	"time"
)

var serverIDCounter uint64

// newServerID generates a Client ID for anonymous (empty-id) CleanSession=1
// connections. Values stay within the documented MQTT ID charset.
func newServerID() string {
	n := atomic.AddUint64(&serverIDCounter, 1)
	return fmt.Sprintf("srv%020d", n)
}

// conn is one live MQTT transport. Outbound writes are serialized by writeMu
// so the read loop, resumption and keep-alive machinery never interleave bytes
// on the wire.
type conn struct {
	broker *Broker
	raw    net.Conn
	br     *bufio.Reader

	writeMu sync.Mutex
	closed  chan struct{}
	once    sync.Once
}

func (c *conn) writePacket(b []byte) bool {
	select {
	case <-c.closed:
		return false
	default:
	}
	c.writeMu.Lock()
	defer c.writeMu.Unlock()
	if _, err := c.raw.Write(b); err != nil {
		return false
	}
	return true
}

func (c *conn) close() { c.once.Do(func() { close(c.closed); _ = c.raw.Close() }) }

// Serve accepts MQTT transports on ln until the listener is closed.
func (b *Broker) Serve(ln net.Listener) error {
	b.mu.Lock()
	b.listener = ln
	b.mu.Unlock()
	for {
		raw, err := ln.Accept()
		if err != nil {
			b.mu.Lock()
			closed := b.closed
			b.mu.Unlock()
			if closed {
				return nil
			}
			return err
		}
		b.wg.Add(1)
		go func() {
			defer b.wg.Done()
			handleConn(b, raw)
		}()
	}
}

// Close stops listening and closes every live transport.
func (b *Broker) Close() error {
	b.mu.Lock()
	if b.closed {
		b.mu.Unlock()
		return nil
	}
	b.closed = true
	ln := b.listener
	var conns []*conn
	for _, s := range b.sessions {
		if s.conn != nil {
			conns = append(conns, s.conn)
		}
	}
	b.mu.Unlock()
	if ln != nil {
		_ = ln.Close()
	}
	for _, c := range conns {
		c.close()
	}
	b.wg.Wait()
	return nil
}

func handleConn(b *Broker, raw net.Conn) {
	c := &conn{
		broker: b,
		raw:    raw,
		br:     bufio.NewReaderSize(raw, 4096),
		closed: make(chan struct{}),
	}
	defer c.close()
	c.run()
}

// fatal writes an optional CONNACK, closes the transport and ends the loop.
func (c *conn) fatal(b []byte) {
	if b != nil {
		_ = c.writePacket(b)
	}
	c.close()
}

func keepAliveDeadline(keepAlive uint16) time.Time {
	if keepAlive == 0 {
		return time.Time{} // disabled
	}
	// The Server MUST disconnect if no packet arrives within 1.5 * KeepAlive
	// seconds (MQTT 3.1.1 §3.1.2-10).
	d := time.Duration(keepAlive)*time.Second + time.Duration(keepAlive)*time.Second/2
	return time.Now().Add(d)
}

func (c *conn) run() {
	b := c.broker

	// First packet MUST be CONNECT (§3.1.0-1). The read deadline protects the
	// server from idle half-open clients during the handshake.
	_ = c.raw.SetReadDeadline(time.Now().Add(30 * time.Second))
	p, err := readPacket(c.br)
	if err != nil {
		var bp badProtoErr
		if errors.As(err, &bp) {
			c.fatal(connackPacket(false, rcBadProto))
			return
		}
		var bc badClientIDErr
		if errors.As(err, &bc) {
			c.fatal(connackPacket(false, rcBadID))
			return
		}
		// Malformed framing before CONNACK: close without a response.
		return
	}
	if p.Type != TypeCONNECT {
		return
	}

	// Protocol level must be MQTT 3.1.1 (4).
	if p.ProtoLevel != 4 {
		c.fatal(connackPacket(false, rcBadProto))
		return
	}
	// Will messages are explicitly outside this subset. Send a CONNACK with
	// return code 5 (not authorized) and then close the transport, making the
	// limitation explicit per the task requirements.
	if p.WillFlag {
		c.fatal(connackPacket(false, rcNotAuthorized))
		return
	}

	s, sessionPresent, keepAlive := b.connect(p, c)
	if !c.writePacket(connackPacket(sessionPresent, rcAccepted)) {
		b.disconnect(s, c)
		return
	}

	_ = c.raw.SetReadDeadline(keepAliveDeadline(keepAlive))

	// Resume delivery: unacknowledged packets go out again with DUP=1 and
	// their ORIGINAL packet IDs; queued (never-sent) messages are fresh sends.
	b.resume(s)

	for {
		p, err := readPacket(c.br)
		if err != nil {
			b.disconnect(s, c)
			return
		}
		_ = c.raw.SetReadDeadline(keepAliveDeadline(keepAlive))
		switch p.Type {
		case TypeCONNECT:
			// A second CONNECT on an established transport is a protocol
			// violation (§3.1.0-2): close the connection.
			b.disconnect(s, c)
			return

		case TypePUBLISH:
			if p.QoS == 2 {
				// QoS 2 publication is unsupported in this subset.
				b.disconnect(s, c)
				return
			}
			c.handlePublish(s, p)

		case TypePUBACK:
			b.handlePuback(s, p.PacketID)

		case TypeSUBSCRIBE:
			c.handleSubscribe(s, p)

		case TypeUNSUBSCRIBE:
			c.handleUnsubscribe(s, p)

		case TypePINGREQ:
			if !c.writePacket(pingrespPacket()) {
				b.disconnect(s, c)
				return
			}

		case TypeDISCONNECT:
			b.clientDisconnect(s, c)
			return

		default:
			// PUBREC(5)/PUBREL(6)/PUBCOMP(7) are QoS 2 packets and any other
			// value is reserved: explicitly unsupported, close immediately.
			b.disconnect(s, c)
			return
		}
	}
}

// connect registers the new transport against its session, applying
// CleanSession semantics and takeover (§3.1.4, §3.14).
func (b *Broker) connect(p *Packet, c *conn) (s *session, sessionPresent bool, keepAlive uint16) {
	id := p.ClientID
	if id == "" {
		// Empty Client ID + CleanSession=1: the server assigns an identifier.
		id = newServerID()
	}
	b.mu.Lock()
	defer b.mu.Unlock()

	if !p.CleanStart {
		if existing, ok := b.sessions[id]; ok && !existing.clean {
			s = existing
			sessionPresent = true
			if existing.conn != nil {
				// Take over: kick the previous transport (§3.1.4-2).
				existing.conn.close()
				existing.conn = nil
			}
		} else {
			s = newSession(id, false)
			b.sessions[id] = s
			b.persistLocked()
		}
	} else {
		// CleanSession=1 discards any session stored for this Client ID.
		if old, ok := b.sessions[id]; ok {
			if old.conn != nil {
				old.conn.close()
			}
			delete(b.sessions, id)
			b.persistLocked()
		}
		s = newSession(id, true)
		b.sessions[id] = s
	}
	s.conn = c
	return s, sessionPresent, p.KeepAlive
}

// resume redelivers inflight messages (DUP=1, original packet IDs) and drains
// the offline queue as fresh (DUP=0, newly allocated packet IDs) messages.
// Delivery state is made durable BEFORE the bytes are written, so a crash in
// between can only cause a duplicate, never a loss.
func (b *Broker) resume(s *session) {
	b.mu.Lock()
	if s.conn == nil {
		b.mu.Unlock()
		return
	}
	var writes []plannedWrite
	for _, pid := range append([]uint16(nil), s.order...) {
		in, ok := s.inflight[pid]
		if !ok {
			continue
		}
		writes = append(writes, plannedWrite{
			s: s, c: s.conn,
			pkt: publishPacket(true, 1, false, in.msg.topic, pid, in.msg.payload),
		})
	}
	// Fresh sends for queued messages: allocate packet IDs and record them as
	// inflight before sending; remove them from the durable queue.
	for len(s.queue) > 0 {
		m := s.queue[0]
		pid, ok := s.allocPID()
		if !ok {
			break // packet-ID space full; leave the rest queued
		}
		s.inflight[pid] = &inflight{pid: pid, msg: m}
		s.order = append(s.order, pid)
		s.queue = s.queue[1:]
		writes = append(writes, plannedWrite{
			s: s, c: s.conn,
			pkt: publishPacket(false, 1, false, m.topic, pid, m.payload),
		})
	}
	b.persistLocked()
	b.mu.Unlock()

	b.flushWrites(writes)
}

// handlePublish processes an inbound PUBLISH (QoS 0 or 1).
//
// For QoS 1 the inbound packet ID is the publisher's identifier and is scoped
// to THIS TCP connection/session: it is unrelated to the packet IDs the broker
// allocates for its own outbound deliveries. The broker acknowledges (PUBACK)
// and then fans out. A redelivered inbound PUBLISH (DUP=1, same ID) is simply
// acknowledged again and delivered again, which is correct at-least-once
// behavior: subscribers may see duplicates and must dedupe themselves.
func (c *conn) handlePublish(s *session, p *Packet) {
	b := c.broker
	m := message{topic: p.Topic, qos: p.QoS, payload: p.Payload}

	b.mu.Lock()
	puback := []byte(nil)
	if p.QoS == 1 {
		puback = pubackPacket(p.PacketID)
	}
	writes, _ := b.planDeliveryLocked(m)
	b.persistLocked()
	cb := b.OnPublish
	b.mu.Unlock()

	// Send the PUBACK first so the publisher stops retrying, then deliver to
	// subscribers. Both states above are already durable.
	if puback != nil && !c.writePacket(puback) {
		b.disconnect(s, c)
		return
	}
	b.flushWrites(writes)
	if cb != nil {
		cb(s.clientID, p.Topic, p.QoS, p.Payload)
	}
}

// handlePuback completes an OUTBOUND QoS 1 delivery: the packet ID is freed
// and the inflight message is forgotten. Unknown IDs are ignored.
func (b *Broker) handlePuback(s *session, pid uint16) {
	b.mu.Lock()
	if _, ok := s.inflight[pid]; ok {
		delete(s.inflight, pid)
		out := s.order[:0]
		for _, id := range s.order {
			if id != pid {
				out = append(out, id)
			}
		}
		s.order = out
		b.persistLocked()
	}
	b.mu.Unlock()
}

func (c *conn) handleSubscribe(s *session, p *Packet) {
	b := c.broker
	codes := make([]byte, len(p.Filters))
	b.mu.Lock()
	for i, f := range p.Filters {
		switch {
		case f.QoS > 2 || !validFilter(f.Filter):
			codes[i] = 0x80 // failure return code (§3.8.3.1)
		default:
			grant := f.QoS
			if grant > 1 {
				grant = 1 // this subset's maximum granted QoS is 1
			}
			s.subs[f.Filter] = grant
			codes[i] = grant
		}
	}
	b.persistLocked()
	b.mu.Unlock()
	if !c.writePacket(subackPacket(p.PacketID, codes)) {
		b.disconnect(s, c)
	}
}

func (c *conn) handleUnsubscribe(s *session, p *Packet) {
	b := c.broker
	b.mu.Lock()
	for _, f := range p.Unsub {
		delete(s.subs, f)
	}
	b.persistLocked()
	b.mu.Unlock()
	if !c.writePacket(unsubackPacket(p.PacketID)) {
		b.disconnect(s, c)
	}
}

// disconnect handles an ungraceful transport loss (network error, keep-alive
// timeout or protocol violation). It only mutates state when c is still the
// session's current transport, so an old goroutine cannot clobber a connection
// that took over the same Client ID.
func (b *Broker) disconnect(s *session, c *conn) {
	b.mu.Lock()
	cur, ok := b.sessions[s.clientID]
	if !ok || cur != s || s.conn != c {
		b.mu.Unlock()
		return
	}
	if s.clean {
		delete(b.sessions, s.clientID)
	} else {
		s.conn = nil
	}
	b.persistLocked()
	b.mu.Unlock()
	c.close()
}

// clientDisconnect handles a client-sent DISCONNECT packet. Clean sessions are
// deleted; persistent sessions retain subscriptions, inflight and queued
// messages for the next connection (§3.14).
func (b *Broker) clientDisconnect(s *session, c *conn) {
	b.mu.Lock()
	cur, ok := b.sessions[s.clientID]
	if !ok || cur != s || s.conn != c {
		b.mu.Unlock()
		return
	}
	if s.clean {
		delete(b.sessions, s.clientID)
	} else {
		s.conn = nil
	}
	b.persistLocked()
	b.mu.Unlock()
	c.close()
}
