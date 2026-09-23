package broker

import (
	"time"

	"mqttsub/internal/mqtt"
)

// pump owns outbound delivery for one live connection.
//
// Delivery model (at-least-once):
//
//  1. On start, every inflight QoS1 message is redelivered with DUP=1,
//     oldest first. Inflight messages survive disconnect/restart, so a
//     lost PUBACK leads to redelivery when the client returns.
//  2. The session pending FIFO is then drained. QoS0 messages are sent
//     directly; QoS1 messages get a per-session packet id assigned and
//     move into inflight.
//  3. While connected, an optional retry timer re-sends still-unacked
//     inflight messages with DUP=1, modelling the "PUBACK lost while
//     still connected" case without waiting for a reconnect.
//
// Exactly one pump runs per live connection. The broker lock is held only
// while inspecting/mutating session state, never across the network
// write, so one slow consumer never blocks routing.
func (c *Conn) pump() {
	if !c.replayInflight() {
		return
	}

	var timerCh <-chan time.Time
	var timer *time.Timer
	if d := c.broker.retry; d > 0 {
		timer = time.NewTimer(d)
		timerCh = timer.C
		defer timer.Stop()
	}

	for {
		if !c.drainPending() {
			return
		}
		select {
		case <-c.stopCh:
			return
		case <-c.kickCh:
			// New pending work.
		case <-timerCh:
			if !c.replayInflight() {
				return
			}
			if timer != nil {
				timer.Reset(c.broker.retry)
			}
		}
	}
}

// replayInflight resends all unacked QoS1 messages, DUP=1, send order.
// It returns false when the connection is gone.
func (c *Conn) replayInflight() bool {
	b := c.broker
	b.mu.Lock()
	ids := append([]uint16(nil), c.sess.inflightOrder...)
	out := make([]*Message, 0, len(ids))
	for _, id := range ids {
		if m := c.sess.inflight[id]; m != nil {
			out = append(out, m)
		}
	}
	b.mu.Unlock()

	for _, m := range out {
		pkt := mqtt.EncodePublish(&mqtt.PublishPacket{
			Dup: true, QoS: 1,
			Topic: m.Topic, PacketID: m.PacketID, Payload: m.Payload,
		})
		if err := c.write(pkt); err != nil {
			return false
		}
	}
	return true
}

// drainPending sends queued messages until pending is empty. Returns false
// when the connection is gone. A message popped from pending but not
// written is stashed on the conn and rolled back to the head of pending
// by cleanup, so nothing is lost and FIFO order is preserved.
func (c *Conn) drainPending() bool {
	for {
		b := c.broker
		b.mu.Lock()
		if b.closed {
			b.mu.Unlock()
			return false
		}
		if len(c.sess.pending) == 0 {
			b.mu.Unlock()
			return true
		}
		m := c.sess.pending[0]
		c.sess.pending = c.sess.pending[1:]

		pkt := &mqtt.PublishPacket{Topic: m.Topic, QoS: m.QoS, Payload: m.Payload}
		if m.QoS == 1 {
			id := c.allocPIDLocked()
			if id == 0 {
				// Every packet id is outstanding; park the message
				// back at the head and wait for an ack to free one.
				c.sess.pending = append([]*Message{m}, c.sess.pending...)
				c.sess.dirty = true
				b.markDirtyLocked()
				b.mu.Unlock()
				select {
				case <-c.stopCh:
					return false
				case <-time.After(20 * time.Millisecond):
				case <-c.kickCh:
				}
				continue
			}
			m.PacketID = id
			c.sess.inflight[id] = m
			c.sess.inflightOrder = append(c.sess.inflightOrder, id)
			pkt.PacketID = id
		}
		c.sess.dirty = true
		b.markDirtyLocked()
		b.mu.Unlock()

		if err := c.write(mqtt.EncodePublish(pkt)); err != nil {
			// Connection dying. QoS1 is safe in inflight; QoS0 has no
			// guarantee but we still hand it back to cleanup for the
			// offline queue of a durable session.
			if m.QoS == 0 {
				c.unwritten = m
			}
			return false
		}
	}
}

// rollbackUnwritten re-attaches a popped-but-not-sent message to the head
// of the pending queue during cleanup (before the session goes offline).
func (c *Conn) rollbackUnwritten() {
	if c.unwritten == nil {
		return
	}
	c.sess.pending = append([]*Message{c.unwritten}, c.sess.pending...)
	c.unwritten = nil
}

// allocPIDLocked returns the next free packet id 1..65535 for the session,
// or 0 when they are all in flight. Caller holds broker mu.
func (c *Conn) allocPIDLocked() uint16 {
	for range 65535 {
		id := c.sess.nextPID
		c.sess.nextPID++
		if c.sess.nextPID == 0 {
			c.sess.nextPID = 1
		}
		if _, busy := c.sess.inflight[id]; !busy {
			return id
		}
	}
	return 0
}
