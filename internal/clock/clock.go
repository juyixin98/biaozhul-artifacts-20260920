// Package clock provides a controllable time source so that streaming,
// delay and timeout behavior can be tested deterministically.
package clock

import (
	"sync"
	"time"
)

// Clock abstracts time. Production code uses Real; tests use Manual.
type Clock interface {
	Now() time.Time
	// After returns a channel that receives the current time once
	// duration d has elapsed on this clock.
	After(d time.Duration) <-chan time.Time
}

// Real is the wall-clock implementation.
type Real struct{}

func (Real) Now() time.Time                         { return time.Now() }
func (Real) After(d time.Duration) <-chan time.Time { return time.After(d) }

// Manual is a deterministic clock advanced explicitly by tests.
type Manual struct {
	mu     sync.Mutex
	now    time.Time
	timers []*manualTimer
}

type manualTimer struct {
	at time.Time
	ch chan time.Time
}

// NewManual returns a Manual clock starting at start.
func NewManual(start time.Time) *Manual {
	return &Manual{now: start}
}

func (m *Manual) Now() time.Time {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.now
}

func (m *Manual) After(d time.Duration) <-chan time.Time {
	m.mu.Lock()
	defer m.mu.Unlock()
	ch := make(chan time.Time, 1)
	t := &manualTimer{at: m.now.Add(d), ch: ch}
	m.timers = append(m.timers, t)
	m.fireLocked()
	return ch
}

// Advance moves the clock forward by d and fires any timers due.
func (m *Manual) Advance(d time.Duration) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.now = m.now.Add(d)
	m.fireLocked()
}

func (m *Manual) fireLocked() {
	remaining := m.timers[:0]
	for _, t := range m.timers {
		if !t.at.After(m.now) {
			t.ch <- m.now
		} else {
			remaining = append(remaining, t)
		}
	}
	m.timers = remaining
}

// Pending reports how many timers are still waiting. Useful in tests
// to assert a producer is blocked on a delay.
func (m *Manual) Pending() int {
	m.mu.Lock()
	defer m.mu.Unlock()
	return len(m.timers)
}
