// Package pool implements a bounded-queue worker pool whose worker count can
// be adjusted at runtime. Shrink requests never interrupt a running task: the
// selected workers finish their current task first.
package pool

import (
	"sync"
	"time"
)

// Clock abstracts wall-clock access so scheduling behaviour can be driven
// deterministically from tests.
type Clock interface {
	Now() time.Time
	// NewTimer creates a timer that fires after d, sending on the returned
	// channel. A stopped/garbage-collected timer may be reused via Reset.
	NewTimer(d time.Duration) Timer
}

// Timer is the subset of *time.Timer used by the scheduler.
type Timer interface {
	// C returns the channel on which the expiry time is delivered.
	C() <-chan time.Time
	// Stop prevents the timer from firing. It reports whether the call
	// stopped the timer before it fired (same semantics as time.Timer).
	Stop() bool
	// Reset changes the timer to expire after duration d.
	Reset(d time.Duration) bool
}

// RealClock uses the system clock and real timers.
type RealClock struct{}

// NewRealClock returns a Clock backed by the wall clock.
func NewRealClock() RealClock { return RealClock{} }

func (RealClock) Now() time.Time { return time.Now() }

func (RealClock) NewTimer(d time.Duration) Timer { return &realTimer{t: time.NewTimer(d)} }

type realTimer struct{ t *time.Timer }

func (r *realTimer) C() <-chan time.Time        { return r.t.C }
func (r *realTimer) Stop() bool                 { return r.t.Stop() }
func (r *realTimer) Reset(d time.Duration) bool { return r.t.Reset(d) }

// FakeClock is a manually driven clock for deterministic tests. Timers fire
// only when Advance (or Run) moves the fake time past their deadline.
type FakeClock struct {
	mu     sync.Mutex
	now    time.Time
	seq    int64
	timers map[int64]*fakeTimer
}

// NewFakeClock returns a FakeClock initialised at epoch (time.Time{}).
func NewFakeClock() *FakeClock { return NewFakeClockAt(time.Unix(0, 0).UTC()) }

// NewFakeClockAt returns a FakeClock initialised at t.
func NewFakeClockAt(t time.Time) *FakeClock {
	return &FakeClock{now: t, timers: make(map[int64]*fakeTimer)}
}

func (f *FakeClock) Now() time.Time {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.now
}

func (f *FakeClock) NewTimer(d time.Duration) Timer {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.seq++
	t := &fakeTimer{
		id:       f.seq,
		deadline: f.now.Add(d),
		c:        make(chan time.Time, 1),
		clock:    f,
	}
	f.timers[t.id] = t
	return t
}

// Advance moves the clock forward by d, firing any timers whose deadline has
// been reached (in deadline order). Timers that fire while the clock is being
// advanced are rescheduled/observed consistently with moving forward once.
func (f *FakeClock) Advance(d time.Duration) {
	f.mu.Lock()
	f.now = f.now.Add(d)
	// Collect due timers under the lock, then release before delivering so
	// that timer callbacks (which may touch the clock) cannot deadlock.
	type due struct {
		deadline time.Time
		t        *fakeTimer
	}
	var dueList []due
	for _, t := range f.timers {
		if !t.stopped && !t.deadline.After(f.now) {
			dueList = append(dueList, due{t.deadline, t})
		}
	}
	// sort by deadline, then id, without importing sort (small lists)
	for i := 1; i < len(dueList); i++ {
		for j := i; j > 0 && (dueList[j].deadline.Before(dueList[j-1].deadline) ||
			(dueList[j].deadline.Equal(dueList[j-1].deadline) && dueList[j].t.id < dueList[j-1].t.id)); j-- {
			dueList[j], dueList[j-1] = dueList[j-1], dueList[j]
		}
	}
	for _, e := range dueList {
		delete(f.timers, e.t.id)
	}
	f.mu.Unlock()
	for _, e := range dueList {
		// C is buffered (cap 1); if a previous value is somehow still there,
		// drop it so the fire is never lost.
		select {
		case e.t.c <- e.deadline:
		default:
			select {
			case <-e.t.c:
			default:
			}
			select {
			case e.t.c <- e.deadline:
			default:
			}
		}
	}
}

// PendingTimers reports how many timers are registered (including stopped ones
// that have not been re-registered).
func (f *FakeClock) PendingTimers() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	n := 0
	for _, t := range f.timers {
		if !t.stopped {
			n++
		}
	}
	return n
}

type fakeTimer struct {
	id       int64
	deadline time.Time
	c        chan time.Time
	stopped  bool
	clock    *FakeClock
}

func (t *fakeTimer) C() <-chan time.Time { return t.c }

func (t *fakeTimer) Stop() bool {
	t.clock.mu.Lock()
	defer t.clock.mu.Unlock()
	if t.stopped {
		return false
	}
	t.stopped = true
	_, ok := t.clock.timers[t.id]
	if ok {
		delete(t.clock.timers, t.id)
	}
	return ok
}

func (t *fakeTimer) Reset(d time.Duration) bool {
	t.clock.mu.Lock()
	defer t.clock.mu.Unlock()
	_, existed := t.clock.timers[t.id]
	t.stopped = false
	t.deadline = t.clock.now.Add(d)
	t.clock.timers[t.id] = t
	return existed
}
