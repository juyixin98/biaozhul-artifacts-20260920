package scheduler

import (
	"runtime"
	"sort"
	"sync"
	"time"
)

// Clock abstracts time so scheduling logic is deterministic under test.
type Clock interface {
	Now() time.Time
	// NewTimer creates a timer firing after d. Reset/Stop work like time.Timer.
	NewTimer(d time.Duration) Timer
	// AfterFunc registers f to run d after the clock advances past the
	// deadline; the returned Timer can Reset or cancel it.
	AfterFunc(d time.Duration, f func()) Timer
}

// Timer is a subset of *time.Timer.
type Timer interface {
	// C fires when the timer expires (nil channel before the first fire and
	// after Stop).
	C() <-chan time.Time
	// Stop prevents the timer from firing. It reports whether the call
	// stopped a pending timer.
	Stop() bool
	// Reset changes the expiry to d from the current clock time.
	Reset(d time.Duration) bool
}

// ---------------------------------------------------------------------------
// Real clock
// ---------------------------------------------------------------------------

type realClock struct{}

// NewRealClock returns a clock backed by wall time and real timers.
func NewRealClock() Clock { return realClock{} }

func (realClock) Now() time.Time { return time.Now() }

func (realClock) NewTimer(d time.Duration) Timer {
	return &realTimer{t: time.NewTimer(d)}
}

func (realClock) AfterFunc(d time.Duration, f func()) Timer {
	return &realTimer{t: time.AfterFunc(d, f)}
}

type realTimer struct {
	t *time.Timer
}

func (r *realTimer) C() <-chan time.Time { return r.t.C }
func (r *realTimer) Stop() bool          { return r.t.Stop() }
func (r *realTimer) Reset(d time.Duration) bool {
	return r.t.Reset(d)
}

// ---------------------------------------------------------------------------
// Fake clock (deterministic, virtual time)
// ---------------------------------------------------------------------------

type fakeTimer struct {
	deadline time.Time
	fn       func()         // nil for NewTimer style
	ch       chan time.Time // non-nil for NewTimer style
	fired    bool
	stopped  bool
	seq      int64
	clock    *FakeClock
}

// FakeClock is a manually driven clock. Advance runs all due timers (in
// deadline, then registration order); AfterFunc callbacks execute synchronously
// on the goroutine that called Advance, so simulated time is fully
// deterministic.
type FakeClock struct {
	mu      sync.Mutex
	now     time.Time
	timers  map[*fakeTimer]struct{}
	nextSeq int64
	firing  bool // true while Advance is dispatching a due group
}

// NewFakeClock returns a fake clock starting at t (defaults to a fixed
// 2026-01-01 UTC instant when t is zero).
func NewFakeClock(t time.Time) *FakeClock {
	if t.IsZero() {
		t = time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	}
	return &FakeClock{
		now:    t,
		timers: make(map[*fakeTimer]struct{}),
	}
}

// Now returns the current virtual time. It blocks while an Advance call is
// dispatching the timer group at that instant, so a consumer can never
// observe "time has reached T but T's timers have not fired" — advancing the
// clock and firing due timers are atomic from every observer's point of view.
func (c *FakeClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

// waitNotFiringLocked blocks until the clock is between dispatch groups.
// Caller must hold c.mu.
func (c *FakeClock) waitNotFiringLocked() {
	for c.firing {
		c.mu.Unlock()
		runtime.Gosched()
		c.mu.Lock()
	}
}

// NewTimer creates a channel timer.
func (c *FakeClock) NewTimer(d time.Duration) Timer {
	return c.newTimer(d, nil)
}

// AfterFunc registers a callback timer.
func (c *FakeClock) AfterFunc(d time.Duration, f func()) Timer {
	return c.newTimer(d, f)
}

func (c *FakeClock) newTimer(d time.Duration, f func()) *fakeTimer {
	if d <= 0 {
		d = 1 // zero-duration timers are not a supported scheduling input
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	c.nextSeq++
	t := &fakeTimer{
		deadline: c.now.Add(d),
		fn:       f,
		seq:      c.nextSeq,
		clock:    c,
	}
	if f == nil {
		t.ch = make(chan time.Time, 1)
	}
	c.timers[t] = struct{}{}
	return t
}

func (t *fakeTimer) C() <-chan time.Time { return t.ch }

func (t *fakeTimer) Stop() bool {
	t.clock.mu.Lock()
	defer t.clock.mu.Unlock()
	pending := !t.stopped && !t.fired
	t.stopped = true
	if pending {
		delete(t.clock.timers, t)
	}
	return pending
}

func (t *fakeTimer) Reset(d time.Duration) bool {
	if d <= 0 {
		d = 1
	}
	t.clock.mu.Lock()
	defer t.clock.mu.Unlock()
	pending := !t.stopped && !t.fired
	t.stopped = false
	t.fired = false
	t.deadline = t.clock.now.Add(d)
	t.clock.timers[t] = struct{}{}
	return pending
}

// Advance moves virtual time to now+d and fires every timer whose deadline
// falls in (oldNow, newNow]. Timers armed by callbacks for a deadline <=
// newNow also fire within the same call, matching time.AfterFunc semantics.
//
// Time is advanced one distinct deadline at a time. While the callback group
// for a deadline is being dispatched, Now() blocks: reaching virtual time T
// and firing everything due at T is an atomic observation, so no consumer can
// run a scheduling pass at "T" before T's timers (e.g. task completions) have
// been delivered.
func (c *FakeClock) Advance(d time.Duration) {
	c.mu.Lock()
	c.waitNotFiringLocked()
	target := c.now.Add(d)

	for {
		next, have := c.earliestDeadlineLocked(target)
		if !have {
			c.now = target
			c.mu.Unlock()
			return
		}

		group := c.groupAtLocked(next)
		// Jump to the deadline and mark dispatch in progress atomically.
		c.now = next
		c.firing = true
		for _, t := range group {
			t.fired = true
			delete(c.timers, t)
		}
		c.mu.Unlock()

		for _, t := range group {
			if t.fn != nil {
				t.fn() // synchronous, in registration order
			} else {
				select {
				case t.ch <- next:
				default:
				}
			}
		}

		c.mu.Lock()
		c.firing = false
	}
}

// earliestDeadlineLocked returns the smallest timer deadline <= up.
// Caller must hold c.mu.
func (c *FakeClock) earliestDeadlineLocked(up time.Time) (time.Time, bool) {
	var next time.Time
	have := false
	for t := range c.timers {
		if t.deadline.After(up) {
			continue
		}
		if !have || t.deadline.Before(next) {
			next, have = t.deadline, true
		}
	}
	return next, have
}

// groupAtLocked returns all timers expiring exactly at t, in registration
// order. Caller must hold c.mu.
func (c *FakeClock) groupAtLocked(t time.Time) []*fakeTimer {
	var group []*fakeTimer
	for tm := range c.timers {
		if tm.deadline.Equal(t) {
			group = append(group, tm)
		}
	}
	sort.Slice(group, func(i, j int) bool { return group[i].seq < group[j].seq })
	return group
}
