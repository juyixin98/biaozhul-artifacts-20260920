// Package clock provides an injectable time source so the transport can be
// driven by a real clock in production and a deterministic manual clock in
// tests.
package clock

import (
	"sync"
	"time"
)

// Timer is a single-shot countdown, mirroring time.Timer.
type Timer interface {
	C() <-chan time.Time
	Stop() bool
}

// Clock abstracts time.Now and timers.
type Clock interface {
	Now() time.Time
	NewTimer(d time.Duration) Timer
}

// Real is the wall-clock implementation.
type Real struct{}

func (Real) Now() time.Time { return time.Now() }

func (Real) NewTimer(d time.Duration) Timer { return &realTimer{t: time.NewTimer(d)} }

type realTimer struct{ t *time.Timer }

func (r *realTimer) C() <-chan time.Time { return r.t.C }
func (r *realTimer) Stop() bool          { return r.t.Stop() }

// Manual is a deterministic clock that only advances when Advance is called.
// It is safe for concurrent use.
type Manual struct {
	mu     sync.Mutex
	now    time.Time
	timers []*manualTimer
}

// NewManual returns a Manual clock starting at start.
func NewManual(start time.Time) *Manual { return &Manual{now: start} }

func (m *Manual) Now() time.Time {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.now
}

func (m *Manual) NewTimer(d time.Duration) Timer {
	t := &manualTimer{m: m, at: m.Now().Add(d), c: make(chan time.Time, 1)}
	m.mu.Lock()
	m.timers = append(m.timers, t)
	m.mu.Unlock()
	m.fireDue()
	return t
}

// Advance moves the clock forward by d and fires every timer that came due.
func (m *Manual) Advance(d time.Duration) {
	m.mu.Lock()
	m.now = m.now.Add(d)
	m.mu.Unlock()
	m.fireDue()
}

// fireDue fires (and removes) every timer whose deadline has passed.
// Each timer fires at most once; its channel has capacity 1, so the send
// never blocks.
func (m *Manual) fireDue() {
	m.mu.Lock()
	now := m.now
	keep := m.timers[:0]
	var fire []*manualTimer
	for _, t := range m.timers {
		switch {
		case t.stopped:
			// drop
		case !t.at.After(now):
			fire = append(fire, t)
		default:
			keep = append(keep, t)
		}
	}
	m.timers = keep
	m.mu.Unlock()
	for _, t := range fire {
		t.c <- now
	}
}

type manualTimer struct {
	m       *Manual
	at      time.Time
	c       chan time.Time
	stopped bool
}

func (t *manualTimer) C() <-chan time.Time { return t.c }

func (t *manualTimer) Stop() bool {
	t.m.mu.Lock()
	defer t.m.mu.Unlock()
	if t.stopped {
		return false
	}
	t.stopped = true
	return true
}
