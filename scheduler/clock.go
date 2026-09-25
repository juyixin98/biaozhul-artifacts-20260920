package scheduler

import (
	"sync"
	"time"
)

// Clock abstracts time so that timed operations can be tested
// deterministically. NewTimer mirrors time.NewTimer: the returned
// channel fires at or after the requested instant.
type Clock interface {
	Now() time.Time
	NewTimer(d time.Duration) Timer
}

// Timer is a cancellable one-shot timer.
type Timer interface {
	C() <-chan time.Time
	// Stop prevents the timer from firing. It reports whether the
	// call stopped the timer before it fired (same contract as
	// time.Timer.Stop: a false result does not mean the timer
	// already fired, only that the timer was stopped or expired).
	Stop() bool
}

// realTimer adapts *time.Timer (whose C is a field) to the Timer
// interface.
type realTimer struct{ t *time.Timer }

func (r realTimer) C() <-chan time.Time { return r.t.C }
func (r realTimer) Stop() bool          { return r.t.Stop() }

// RealClock uses the wall clock.
type RealClock struct{}

// NewRealClock returns the wall-clock implementation.
func NewRealClock() RealClock { return RealClock{} }

// Now returns the current wall-clock time.
func (RealClock) Now() time.Time { return time.Now() }

// NewTimer returns a real timer from the standard library.
func (RealClock) NewTimer(d time.Duration) Timer {
	return realTimer{t: time.NewTimer(d)}
}

// mockTimer is a Timer controlled by a MockClock.
type mockTimer struct {
	deadline time.Time
	ch       chan time.Time
	clock    *MockClock

	mu      sync.Mutex
	stopped bool
	fired   bool
}

// C returns the channel on which the fire time is delivered.
func (t *mockTimer) C() <-chan time.Time { return t.ch }

// Stop cancels the timer. It returns true if the timer was stopped
// before it was due to fire.
func (t *mockTimer) Stop() bool {
	t.clock.mu.Lock()
	defer t.clock.mu.Unlock()
	if t.fired || t.stopped {
		return false
	}
	t.stopped = true
	t.clock.removeTimer(t)
	return true
}

// MockClock is a manually advanced Clock. Timers never fire until
// Advance moves the clock to (or past) their deadline.
type MockClock struct {
	mu     sync.Mutex
	now    time.Time
	timers []*mockTimer
}

// NewMockClock returns a mock clock anchored at the given start time
// (time.Unix(0,0) when start is the zero value).
func NewMockClock(start time.Time) *MockClock {
	if start.IsZero() {
		start = time.Unix(0, 0)
	}
	return &MockClock{now: start}
}

// Now returns the mock current time.
func (c *MockClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

// NewTimer schedules a mock timer with duration d.
func (c *MockClock) NewTimer(d time.Duration) Timer {
	c.mu.Lock()
	defer c.mu.Unlock()
	t := &mockTimer{
		deadline: c.now.Add(d),
		ch:       make(chan time.Time, 1),
		clock:    c,
	}
	if d <= 0 {
		t.fired = true
		t.ch <- c.now
	} else {
		c.timers = append(c.timers, t)
	}
	return t
}

// removeTimer removes t from the pending set. Callers hold c.mu.
func (c *MockClock) removeTimer(t *mockTimer) {
	for i, x := range c.timers {
		if x == t {
			c.timers = append(c.timers[:i], c.timers[i+1:]...)
			return
		}
	}
}

// Advance moves the mock clock forward by d and synchronously fires
// every due timer whose deadline is not after the new instant, in
// deadline order. Channels are buffered with capacity 1, so firing
// never blocks on the receiver. Timers stopped (or scheduled with a
// non-positive duration, which fire immediately on creation) are
// skipped.
func (c *MockClock) Advance(d time.Duration) []time.Time {
	if d <= 0 {
		return nil
	}
	c.mu.Lock()
	c.now = c.now.Add(d)
	due := make([]*mockTimer, 0)
	rest := c.timers[:0]
	for _, t := range c.timers {
		if !t.deadline.After(c.now) {
			due = append(due, t)
		} else {
			rest = append(rest, t)
		}
	}
	c.timers = rest
	c.mu.Unlock()

	fired := make([]time.Time, 0, len(due))
	// Stable order: insertion order is effectively deadline order for
	// timers scheduled with the same advancing clock; sort cheaply
	// with an insertion pass to guarantee deadline order.
	for i := 1; i < len(due); i++ {
		for j := i; j > 0 && due[j-1].deadline.After(due[j].deadline); j-- {
			due[j-1], due[j] = due[j], due[j-1]
		}
	}
	for _, t := range due {
		t.mu.Lock()
		active := !t.stopped && !t.fired
		if active {
			t.fired = true
		}
		t.mu.Unlock()
		if active {
			select {
			case t.ch <- t.deadline:
			default:
			}
			fired = append(fired, t.deadline)
		}
	}
	return fired
}

// Pending reports how many active timers are scheduled in the future.
func (c *MockClock) Pending() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return len(c.timers)
}
