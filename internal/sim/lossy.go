// Package sim wires the transport endpoints together over in-memory or
// real loopback-UDP links, with a seeded fault-injection layer (drop,
// duplicate, reorder) and stale-generation packet injection.
package sim

import (
	"math/rand"
	"sync"
)

// FaultStats counts what the fault-injection layer did, per direction.
type FaultStats struct {
	Sent       int // packets handed to the layer
	Dropped    int
	Duplicated int
	Reordered  int // packets held back and released after a later packet
}

// Lossy is a deterministic, seeded fault-injection layer wrapped around a
// datagram send function. Given the same seed and the same send sequence it
// reproduces the exact same fault pattern.
type Lossy struct {
	rnd *rand.Rand

	drop    float64
	dup     float64
	reorder float64

	held []byte // one packet held back for reordering

	mu sync.Mutex
	st FaultStats
}

// NewLossy builds a Lossy layer; drop/dup/reorder are per-packet
// probabilities whose sum must be < 1.
func NewLossy(seed int64, drop, dup, reorder float64) *Lossy {
	return &Lossy{
		rnd:     rand.New(rand.NewSource(seed)),
		drop:    drop,
		dup:     dup,
		reorder: reorder,
	}
}

// Wrap returns a send function with faults injected. send is the underlying
// datagram sink. The returned function is safe for one sender goroutine per
// direction (the transport's model); internal state is mutex-guarded.
func (l *Lossy) Wrap(send func([]byte) error) func([]byte) error {
	return func(b []byte) error {
		l.mu.Lock()
		l.st.Sent++
		// A previously held packet is released now, after the current
		// packet, so the two go out of order.
		if l.held != nil {
			h := l.held
			l.held = nil
			l.mu.Unlock()
			if err := send(b); err != nil {
				return err
			}
			return send(h)
		}
		r := l.rnd.Float64()
		switch {
		case r < l.drop:
			l.st.Dropped++
			l.mu.Unlock()
			return nil
		case r < l.drop+l.dup:
			l.st.Duplicated++
			l.mu.Unlock()
			if err := send(b); err != nil {
				return err
			}
			return send(b)
		case r < l.drop+l.dup+l.reorder:
			l.st.Reordered++
			l.held = append([]byte(nil), b...)
			l.mu.Unlock()
			return nil
		default:
			l.mu.Unlock()
			return send(b)
		}
	}
}

// Flush releases a packet still held for reordering. Call once the transfer
// is over so no packet stays stuck in the layer.
func (l *Lossy) Flush(send func([]byte) error) {
	l.mu.Lock()
	h := l.held
	l.held = nil
	l.mu.Unlock()
	if h != nil {
		_ = send(h)
	}
}

// Stats returns a snapshot of the fault counters.
func (l *Lossy) Stats() FaultStats {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.st
}
