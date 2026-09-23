package broker

import (
	"bufio"
	"errors"
	"io"
	"net"
	"sync"
	"sync/atomic"
	"time"

	"mqttsub/internal/mqtt"
)

// Conn is one network connection and, after CONNECT, its bound session.
// All session-touching fields are accessed with Broker.mu held.
type Conn struct {
	broker *Broker
	nc     net.Conn
	br     *bufio.Reader

	writeMu    sync.Mutex
	stopCh     chan struct{}
	stopClosed atomic.Bool

	clientID string
	sess     *Session
	will     *mqtt.ConnectPacket // will fields extracted from CONNECT

	kickCh chan struct{}
	clean  bool // client sent DISCONNECT

	// adminDeleted is set by an HTTP DELETE purge: suppress the will
	// message even though the close is abnormal.
	adminDeleted bool

	// unwritten holds a QoS0 message popped from pending but not sent
	// when the connection died; cleanup rolls it back into the queue.
	unwritten *Message
}

func newConn(b *Broker, nc net.Conn) *Conn {
	return &Conn{
		broker: b,
		nc:     nc,
		br:     bufio.NewReaderSize(nc, 64*1024),
		stopCh: make(chan struct{}),
		kickCh: make(chan struct{}, 1),
	}
}

// stop closes stopCh exactly once; pump loops select on it.
func (c *Conn) stop() {
	if c.stopClosed.CompareAndSwap(false, true) {
		close(c.stopCh)
	}
}

// write sends one already-encoded MQTT packet. The mutex serialises writes
// from the pump, ping responses and direct ACKs; closing stopCh unblocks a
// write stuck on a dead socket because nc itself is closed first.
func (c *Conn) write(pkt []byte) error {
	c.writeMu.Lock()
	defer c.writeMu.Unlock()
	if c.stopClosed.Load() {
		return errClosing
	}
	_, err := c.nc.Write(pkt)
	return err
}

var errClosing = errors.New("connection closing")

// kick wakes the bound session's pump (non-blocking; one pending wake is
// enough because the pump always drains the whole pending queue).
func (c *Conn) kick() {
	select {
	case c.kickCh <- struct{}{}:
	default:
	}
}

// serve is the whole per-connection state machine.
func (c *Conn) serve() {
	defer c.cleanup()

	// 1) The very first packet MUST be CONNECT (MQTT-3.1.0-1).
	fr, err := mqtt.ReadFrame(c.br)
	if err != nil {
		return // nothing sent; connection died mid-handshake
	}
	if fr.Type() != mqtt.TypeCONNECT {
		c.protocolError()
		return
	}
	cp, err := mqtt.DecodeConnect(fr.Body)
	if err != nil {
		var codeErr *mqtt.ConnCodeError
		if errors.As(err, &codeErr) {
			// Return-code cases get a CONNACK then close (§3.2.2).
			_ = c.write(mqtt.EncodeConnack(false, codeErr.Code))
		}
		c.protocolError()
		return
	}
	c.clientID = cp.ClientID
	c.will = cp

	// 2) Bind the session (creating/replacing per clean-session rules).
	sessionPresent := c.attach(cp)

	// 3) CONNACK.
	if err := c.write(mqtt.EncodeConnack(sessionPresent, mqtt.ConnAccepted)); err != nil {
		return
	}

	// 4) Keep-alive deadline: 1.5 × keep alive (§3.1.2.10). Zero = none.
	if cp.KeepAlive > 0 {
		deadline := time.Duration(cp.KeepAlive)*time.Second + time.Duration(cp.KeepAlive)*time.Second/2
		_ = c.nc.SetReadDeadline(time.Now().Add(deadline))
	}

	// 5) Pump redelivers inflight, then streams the pending queue.
	go c.pump()

	// 6) Main read loop.
	c.loop(cp.KeepAlive)
}

// attach registers the connection on its session and returns the
// session-present flag to put in CONNACK.
func (c *Conn) attach(cp *mqtt.ConnectPacket) bool {
	b := c.broker
	b.mu.Lock()
	defer b.mu.Unlock()

	existing := b.sessions[cp.ClientID]
	if cp.CleanSession {
		// Any previous session state is discarded (MQTT-3.1.4-2).
		if existing != nil {
			if existing.conn != nil {
				// Take-over: kick the old connection (no will, §3.1.4-3).
				old := existing.conn
				existing.conn = nil
				go func() { old.stop(); _ = old.nc.Close() }()
			}
			delete(b.sessions, cp.ClientID)
			existing = nil
		}
		s := newSession(cp.ClientID, false) // clean sessions are transient
		s.conn = c
		c.sess = s
		b.sessions[cp.ClientID] = s
		return false
	}

	// Durable: resume if present, otherwise create.
	sessionPresent := false
	if existing == nil {
		existing = newSession(cp.ClientID, true)
		b.sessions[cp.ClientID] = existing
	} else {
		sessionPresent = true
	}
	if existing.conn != nil {
		old := existing.conn
		existing.conn = nil
		go func() { old.stop(); _ = old.nc.Close() }()
	}
	existing.conn = c
	c.sess = existing
	if len(existing.inflightOrder) > 0 || len(existing.pending) > 0 {
		c.kick()
	}
	return sessionPresent
}

// loop reads and dispatches packets until DISCONNECT or an error.
func (c *Conn) loop(keepAlive uint16) {
	for {
		fr, err := mqtt.ReadFrame(c.br)
		if err != nil {
			if errors.Is(err, io.EOF) {
				// Half-close without DISCONNECT → abnormal, will fires.
				return
			}
			// Timeout or any malformed framing: protocol error/close.
			if isTimeout(err) {
				return
			}
			c.protocolError()
			return
		}

		if keepAlive > 0 {
			deadline := time.Duration(keepAlive)*time.Second + time.Duration(keepAlive)*time.Second/2
			_ = c.nc.SetReadDeadline(time.Now().Add(deadline))
		}

		switch fr.Type() {
		case mqtt.TypePINGREQ:
			if fr.Flags() != 0 || len(fr.Body) != 0 {
				c.protocolError()
				return
			}
			if err := c.write(mqtt.PacketPingResp); err != nil {
				return
			}
		case mqtt.TypeDISCONNECT:
			if fr.Flags() != 0 || len(fr.Body) != 0 {
				c.protocolError()
				return
			}
			c.clean = true
			return
		case mqtt.TypePUBLISH:
			if !c.handlePublish(fr) {
				c.protocolError()
				return
			}
		case mqtt.TypePUBACK:
			if fr.Flags() != 0 || !c.handlePuback(fr) {
				c.protocolError()
				return
			}
		case mqtt.TypeSUBSCRIBE:
			if !c.handleSubscribe(fr) {
				c.protocolError()
				return
			}
		case mqtt.TypeUNSUBSCRIBE:
			if !c.handleUnsubscribe(fr) {
				c.protocolError()
				return
			}
		case mqtt.TypeCONNECT:
			// Second CONNECT on the same connection is illegal.
			c.protocolError()
			return
		case mqtt.TypePUBREC, mqtt.TypePUBREL, mqtt.TypePUBCOMP,
			mqtt.TypeRESERVED1, mqtt.TypeRESERVED15:
			// QoS 2 flow and reserved types: explicitly unsupported.
			c.protocolError()
			return
		default:
			c.protocolError()
			return
		}
	}
}

// handlePublish processes an inbound PUBLISH; returns false on protocol error.
func (c *Conn) handlePublish(fr *mqtt.Frame) bool {
	flags := fr.Flags()
	p, err := mqtt.DecodePublish(flags, fr.Body)
	if err != nil {
		return false
	}
	if p.QoS == 2 {
		// QoS 2 is in the reserved-but-decoded set; still unsupported.
		return false
	}

	if p.QoS == 1 {
		b := c.broker
		b.mu.Lock()
		// Suppress re-fan-out only for a genuine redelivery: DUP=1 on
		// an identifier we have already answered. We still repeat the
		// PUBACK so the publisher can retire its retry.
		if p.Dup && c.sess.seenInbound[p.PacketID] {
			b.mu.Unlock()
			return c.write(mqtt.EncodePuback(p.PacketID)) == nil
		}
		// DUP=0 is always a fresh delivery. A publisher may reuse a
		// packet id after receiving the earlier PUBACK (MQTT-2.3.1-3),
		// so a seen id with DUP=0 must not be dropped.
		c.sess.seenInbound[p.PacketID] = true
		c.sess.dirty = true
		b.markDirtyLocked()
		b.mu.Unlock()
	}

	// Retain is not supported; the flag is accepted but has no effect.
	if _, err := c.broker.Publish(p.Topic, p.QoS, p.Payload); err != nil {
		return false
	}

	if p.QoS == 1 {
		if err := c.write(mqtt.EncodePuback(p.PacketID)); err != nil {
			return false
		}
	}
	return true
}

// handlePuback clears an outbound inflight message.
func (c *Conn) handlePuback(fr *mqtt.Frame) bool {
	id, err := mqtt.DecodeAck(fr.Body)
	if err != nil {
		return false
	}
	b := c.broker
	b.mu.Lock()
	defer b.mu.Unlock()
	m := c.sess.inflight[id]
	if m == nil {
		// Unknown id: ignored per the tolerant spirit of §3.3.4.
		return true
	}
	delete(c.sess.inflight, id)
	c.sess.inflightOrder = removeUint16(c.sess.inflightOrder, id)
	c.sess.dirty = true
	b.markDirtyLocked()
	return true
}

// handleSubscribe adds subscriptions (downgrading QoS 2 requests to QoS 1)
// and answers SUBACK.
func (c *Conn) handleSubscribe(fr *mqtt.Frame) bool {
	pid, subs, err := mqtt.DecodeSubscribe(fr.Flags(), fr.Body)
	if err != nil {
		return false
	}
	codes := make([]byte, len(subs))
	b := c.broker
	b.mu.Lock()
	for i, s := range subs {
		if !mqtt.ValidFilter(s.Filter) {
			codes[i] = 0x80 // failure
			continue
		}
		qos := s.MaxQoS
		if qos > 1 {
			qos = 1 // QoS 2 not supported: grant at most 1 (§3.8.4)
		}
		c.sess.subs[s.Filter] = qos
		codes[i] = qos
	}
	c.sess.dirty = true
	b.markDirtyLocked()
	b.mu.Unlock()
	return c.write(mqtt.EncodeSuback(pid, codes)) == nil
}

// handleUnsubscribe removes filters and answers UNSUBACK.
func (c *Conn) handleUnsubscribe(fr *mqtt.Frame) bool {
	pid, filters, err := mqtt.DecodeUnsubscribe(fr.Flags(), fr.Body)
	if err != nil {
		return false
	}
	b := c.broker
	b.mu.Lock()
	for _, f := range filters {
		delete(c.sess.subs, f)
	}
	c.sess.dirty = true
	b.markDirtyLocked()
	b.mu.Unlock()
	return c.write(mqtt.EncodeUnsuback(pid)) == nil
}

// protocolError closes the connection immediately with no further packet,
// as mandated for malformed/unexpected packets (§4.8).
func (c *Conn) protocolError() {
	c.stop()
	_ = c.nc.Close()
}

// cleanup detaches the connection from its session, publishes the will on
// abnormal termination, and deletes/keeps the session per clean-session.
func (c *Conn) cleanup() {
	c.stop()
	_ = c.nc.Close()

	b := c.broker
	var will *mqtt.ConnectPacket
	var detached bool

	b.mu.Lock()
	if c.sess != nil && c.sess.conn == c {
		c.sess.conn = nil
		detached = true
		will = c.will
		// Roll back a popped-but-unsent QoS0 message.
		c.rollbackUnwritten()
		if !c.sess.Durable {
			// Clean (transient) sessions are deleted on every
			// disconnect (MQTT-3.1.2-6); their will already fired
			// below when the close was abnormal.
			delete(b.sessions, c.clientID)
		} else {
			c.sess.dirty = true
			b.markDirtyLocked()
		}
	}
	b.mu.Unlock()

	// Will message: only on abnormal disconnect and only if this conn
	// still owned the session (a take-over suppresses it, §3.1.4-3; an
	// administrative purge suppresses it too).
	if detached && !c.clean && !c.adminDeleted && will != nil && will.WillFlag {
		qos := will.WillQoS
		if qos > 1 {
			qos = 1
		}
		_, _ = b.Publish(will.WillTopic, qos, will.WillMsg)
	}
}

func isTimeout(err error) bool {
	var ne net.Error
	return errors.As(err, &ne) && ne.Timeout()
}

// removeUint16 removes the first occurrence of v.
func removeUint16(xs []uint16, v uint16) []uint16 {
	for i, x := range xs {
		if x == v {
			return append(xs[:i], xs[i+1:]...)
		}
	}
	return xs
}
