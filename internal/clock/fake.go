package clock

import (
	"context"
	"sort"
	"sync"
	"time"
)

// FakeClock is a manually driven clock. Time stands still until Advance is
// called, so tests can verify timeout and cancellation behavior with no
// real sleeps.
//
// Timers whose deadline has already passed at creation fire immediately,
// matching time.NewTimer semantics for non-positive durations.
type FakeClock struct {
	mu      sync.Mutex
	now     time.Time
	nextID  uint64
	waiters map[uint64]*waiter
}

type waiter struct {
	id       uint64
	deadline time.Time
	ch       chan time.Time
	stopped  bool
	fired    bool
}

// NewFakeClock returns a fake clock at the given instant.
func NewFakeClock(start time.Time) *FakeClock {
	return &FakeClock{
		now:     start,
		waiters: make(map[uint64]*waiter),
	}
}

// Now reports the simulated time.
func (f *FakeClock) Now() time.Time {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.now
}

// PendingTimers reports how many created timers have neither fired nor been
// stopped. Tests use it to wait until expected timers are registered before
// advancing, keeping fake-clock tests deterministic.
func (f *FakeClock) PendingTimers() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	n := 0
	for _, w := range f.waiters {
		if !w.stopped && !w.fired {
			n++
		}
	}
	return n
}

// Advance moves simulated time forward by d and fires every timer that has
// become due, in deadline order. Durations must be non-negative.
func (f *FakeClock) Advance(d time.Duration) {
	if d < 0 {
		panic("clock: negative Advance")
	}
	f.mu.Lock()
	f.now = f.now.Add(d)
	due := make([]*waiter, 0)
	for _, w := range f.waiters {
		if !w.stopped && !w.fired && !w.deadline.After(f.now) {
			w.fired = true
			due = append(due, w)
		}
	}
	f.mu.Unlock()

	sort.Slice(due, func(i, j int) bool { return due[i].deadline.Before(due[j].deadline) })
	for _, w := range due {
		// time.Timer.C has capacity one; never block a firing goroutine.
		select {
		case w.ch <- w.deadline:
		default:
		}
	}
}

// AdvanceTo moves time to t (which must not be before Now) and fires due timers.
func (f *FakeClock) AdvanceTo(t time.Time) {
	f.mu.Lock()
	d := t.Sub(f.now)
	f.mu.Unlock()
	f.Advance(d)
}

// NewTimer creates a timer driven by the fake clock.
func (f *FakeClock) NewTimer(d time.Duration) Timer {
	f.mu.Lock()
	f.nextID++
	w := &waiter{id: f.nextID, deadline: f.now.Add(d), ch: make(chan time.Time, 1)}
	f.waiters[w.id] = w
	fireNow := !w.deadline.After(f.now)
	if fireNow {
		w.fired = true
	}
	f.mu.Unlock()
	if fireNow {
		w.ch <- w.deadline
	}
	return &fakeTimer{fc: f, id: w.id}
}

// After is the channel form of NewTimer.
func (f *FakeClock) After(d time.Duration) <-chan time.Time {
	return f.NewTimer(d).C()
}

// Sleep blocks until d of simulated time passes or ctx is canceled.
func (f *FakeClock) Sleep(ctx context.Context, d time.Duration) error {
	return sleepOn(ctx, f.NewTimer(d))
}

func (f *FakeClock) stopWaiter(id uint64) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	w, ok := f.waiters[id]
	if !ok || w.fired || w.stopped {
		return false
	}
	w.stopped = true
	return true
}

func (f *FakeClock) resetWaiter(id uint64, d time.Duration) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	w, ok := f.waiters[id]
	if !ok {
		return false
	}
	wasActive := !w.fired && !w.stopped
	w.stopped = false
	w.fired = false
	w.deadline = f.now.Add(d)
	if !w.deadline.After(f.now) {
		w.fired = true
		go func(ch chan time.Time, t time.Time) {
			select {
			case ch <- t:
			default:
			}
		}(w.ch, w.deadline)
	}
	return wasActive
}

type fakeTimer struct {
	fc *FakeClock
	id uint64
}

func (t *fakeTimer) C() <-chan time.Time {
	t.fc.mu.Lock()
	defer t.fc.mu.Unlock()
	return t.fc.waiters[t.id].ch
}

func (t *fakeTimer) Stop() bool                 { return t.fc.stopWaiter(t.id) }
func (t *fakeTimer) Reset(d time.Duration) bool { return t.fc.resetWaiter(t.id, d) }
