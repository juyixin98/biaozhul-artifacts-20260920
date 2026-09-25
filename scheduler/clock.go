package scheduler

import (
	"sort"
	"sync"
	"time"
)

// Clock abstracts wall-clock operations the engine depends on, so retry
// backoff can be driven deterministically in tests.
type Clock interface {
	Now() time.Time
	// NewTimer creates a timer that fires after d. A zero/negative d fires
	// immediately. Timers must be Stop-able; stopped timers never fire.
	NewTimer(d time.Duration) Timer
}

// Timer is a stoppable one-shot timer.
type Timer interface {
	C() <-chan time.Time
	Stop() bool
}

// RealClock uses the host wall clock.
type RealClock struct{}

func (RealClock) Now() time.Time { return time.Now() }

func (RealClock) NewTimer(d time.Duration) Timer {
	return &realTimer{t: time.NewTimer(d)}
}

type realTimer struct{ t *time.Timer }

func (r *realTimer) C() <-chan time.Time { return r.t.C }
func (r *realTimer) Stop() bool          { return r.t.Stop() }

// FakeClock is a manually advanced clock for deterministic tests.
// Advance(fireTime) fires all pending timers whose deadline <= new now, in
// deadline order; zero-duration timers fire on the next Advance (including
// Advance(0)). NewTimer is safe for concurrent use.
type FakeClock struct {
	mu     sync.Mutex
	now    time.Time
	seq    int64
	timers map[int64]*fakeTimer
}

// NewFakeClock starts at t (or time.Unix(0,0) when t is zero).
func NewFakeClock(t time.Time) *FakeClock {
	if t.IsZero() {
		t = time.Unix(0, 0)
	}
	return &FakeClock{now: t, timers: map[int64]*fakeTimer{}}
}

type fakeTimer struct {
	clock    *FakeClock
	id       int64
	deadline time.Time
	ch       chan time.Time
	stopped  bool
	fired    bool
}

func (f *fakeTimer) C() <-chan time.Time { return f.ch }

// Stop prevents the timer from firing. Returns true if it stopped a pending
// timer, mirroring time.Timer.Stop semantics loosely.
func (f *fakeTimer) Stop() bool {
	fc := f.clock
	fc.mu.Lock()
	defer fc.mu.Unlock()
	wasPending := !f.stopped && !f.fired
	f.stopped = true
	if wasPending {
		delete(fc.timers, f.id)
	}
	return wasPending
}

func (f *FakeClock) Now() time.Time {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.now
}

// NewTimer queues a timer at now+d (zero/negative durations fire on the next
// Advance, to avoid recursion when timers are scheduled from inside an
// Advance-driven callback).
func (f *FakeClock) NewTimer(d time.Duration) Timer {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.seq++
	t := &fakeTimer{
		clock:    f,
		id:       f.seq,
		deadline: f.now.Add(d),
		ch:       make(chan time.Time, 1),
	}
	if d <= 0 {
		// Immediate timers are kept in the same map with deadline == now;
		// Advance(0) drains them.
	}
	f.timers[t.id] = t
	return t
}

// Advance moves the clock by d and fires every due timer exactly once, in
// chronological order. Timers newly scheduled during firing (e.g. the next
// zero-duration retry) are picked up in the same call when their deadline is
// <= the new now.
func (f *FakeClock) Advance(d time.Duration) {
	f.mu.Lock()
	target := f.now.Add(d)
	f.now = target

	for {
		type due struct {
			id int64
			t  *fakeTimer
		}
		var ready []due
		for id, t := range f.timers {
			if !t.deadline.After(target) {
				ready = append(ready, due{id, t})
			}
		}
		if len(ready) == 0 {
			break
		}
		sort.Slice(ready, func(i, j int) bool {
			if ready[i].t.deadline.Equal(ready[j].t.deadline) {
				return ready[i].id < ready[j].id
			}
			return ready[i].t.deadline.Before(ready[j].t.deadline)
		})
		first := ready[0]
		t := first.t
		delete(f.timers, first.id)
		t.fired = true
		// Fire without holding the lock so the receiver can re-enter
		// NewTimer/Stop. Buffered channel + fired flag make this safe.
		f.mu.Unlock()
		t.ch <- t.deadline
		f.mu.Lock()
	}
	f.mu.Unlock()
}
