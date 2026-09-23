// Package clock provides replaceable monotonic time sources and delayed-task
// executors. Production code uses RealClock; deterministic tests use FakeClock.
//
// Time is expressed as an Instant: nanoseconds on a monotonic timeline whose
// epoch is implementation defined. Instants are never wall-clock values, so
// differences between instants are immune to wall-clock adjustments.
package clock

import (
	"sort"
	"sync"
	"time"
)

// Instant is a point on a monotonic nanosecond timeline.
type Instant int64

// Clock is a monotonic time source.
type Clock interface {
	Now() Instant
}

// Scheduler runs a function at or after a given monotonic instant.
//
// The returned stop function attempts to cancel the pending call and reports
// whether it succeeded: it returns true only if the function had not started
// executing yet. Stop is safe to call exactly once; subsequent calls are
// no-ops returning false.
type Scheduler interface {
	After(at Instant, fn func()) (stop func() bool)
}

// RealClock implements Clock and Scheduler against wall-clock runtime.
// Now uses time.Now().UnixNano(), whose result carries Go's monotonic-clock
// reading internally; all scheduling math treats it as an opaque instant.
type RealClock struct{}

// NewRealClock returns a Clock+Scheduler backed by real time.
func NewRealClock() RealClock { return RealClock{} }

// Now returns the current monotonic-ish instant in nanoseconds.
func (RealClock) Now() Instant { return Instant(time.Now().UnixNano()) }

// After runs fn when the real clock reaches at.
func (RealClock) After(at Instant, fn func()) func() bool {
	d := time.Duration(int64(at) - time.Now().UnixNano())
	if d < 0 {
		d = 0
	}
	t := time.NewTimer(d)

	var once sync.Once
	done := make(chan struct{})
	stop := make(chan struct{})
	go func() {
		select {
		case <-t.C:
			fn()
		case <-stop:
		}
		close(done)
	}()

	return func() bool {
		stopped := false
		once.Do(func() {
			if t.Stop() {
				stopped = true
				close(stop)
				<-done
			}
		})
		return stopped
	}
}

// FakeClock is a manually driven Clock + Scheduler for deterministic tests.
// Timers never fire on their own: Advance (including Advance(0)) fires every
// timer due at or before the resulting time, in (time, insertion) order.
type FakeClock struct {
	mu     sync.Mutex
	now    int64
	nextID uint64
	timers map[*fakeTimer]struct{}
}

type fakeTimer struct {
	at      int64
	seq     uint64
	fn      func()
	stopped bool
}

// NewFakeClock returns a FakeClock positioned at start.
func NewFakeClock(start Instant) *FakeClock {
	return &FakeClock{
		now:    int64(start),
		timers: make(map[*fakeTimer]struct{}),
	}
}

// Now returns the virtual current time.
func (c *FakeClock) Now() Instant {
	c.mu.Lock()
	defer c.mu.Unlock()
	return Instant(c.now)
}

// Advance moves virtual time forward by d and runs all timers that become due.
// Timers scheduled at instants <= the current time (including timers scheduled
// in the past by a firing job) run on the next due-timer pass, so chains of
// zero-delay timers are drained within a single Advance.
//
// Advance panics on a negative duration: virtual time must not go backwards
// (the limiter treats instants as monotonically non-decreasing).
func (c *FakeClock) Advance(d time.Duration) {
	if d < 0 {
		panic("clock: FakeClock.Advance with negative duration")
	}
	c.mu.Lock()
	target := c.now + int64(d)
	c.mu.Unlock()

	for {
		c.mu.Lock()
		var next *fakeTimer
		for t := range c.timers {
			if t.at <= target && (next == nil || t.at < next.at || (t.at == next.at && t.seq < next.seq)) {
				next = t
			}
		}
		if next == nil {
			c.now = target
			c.mu.Unlock()
			return
		}

		// Step to the exact due instant so jobs observe the ready instant.
		if next.at > c.now {
			c.now = next.at
		}
		due := make([]*fakeTimer, 0, 4)
		for t := range c.timers {
			if t.at <= c.now {
				due = append(due, t)
			}
		}
		sort.Slice(due, func(i, j int) bool {
			a, b := due[i], due[j]
			if a.at != b.at {
				return a.at < b.at
			}
			return a.seq < b.seq
		})
		for _, t := range due {
			delete(c.timers, t)
		}
		c.mu.Unlock()

		for _, t := range due {
			t.fn()
		}
	}
}

// PeekWait returns the duration until the next scheduled timer, or -1 if none.
func (c *FakeClock) PeekWait() time.Duration {
	c.mu.Lock()
	defer c.mu.Unlock()
	var min int64
	found := false
	for t := range c.timers {
		if !found || t.at < min {
			min = t.at
			found = true
		}
	}
	if !found {
		return -1
	}
	return time.Duration(min - c.now)
}

// After registers fn to run at virtual time at.
func (c *FakeClock) After(at Instant, fn func()) func() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.nextID++
	t := &fakeTimer{at: int64(at), seq: c.nextID, fn: fn}
	c.timers[t] = struct{}{}
	return func() bool {
		c.mu.Lock()
		defer c.mu.Unlock()
		if t.stopped {
			return false
		}
		t.stopped = true
		delete(c.timers, t)
		return true
	}
}
