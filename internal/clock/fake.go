package clock

import (
	"sync"
	"time"
)

// Fake is a manually controlled Clock. Timers only fire when the clock is
// advanced, which makes timeout/cancellation races deterministic in tests.
//
// Stopped timers are detached immediately, so repeatedly creating and
// stopping timers on a Fake does not accumulate pending state.
//
// Fake is safe for concurrent use: goroutines may create timers while the
// test goroutine advances the clock.
type Fake struct {
	mu      sync.Mutex
	now     time.Time
	seq     int64
	waiters []*fakeTimer
}

type fakeTimer struct {
	fake    *Fake
	wakeAt  time.Time
	seq     int64
	ch      chan time.Time
	fired   bool
	stopped bool
}

// NewFake returns a fake clock anchored at start.
func NewFake(start time.Time) *Fake {
	return &Fake{now: start}
}

// Now returns the simulated current time.
func (f *Fake) Now() time.Time {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.now
}

// Timer registers a timer. As with time.NewTimer, d <= 0 fires immediately.
func (f *Fake) Timer(d time.Duration) Timer {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.seq++
	t := &fakeTimer{
		fake:   f,
		wakeAt: f.now.Add(d),
		seq:    f.seq,
		ch:     make(chan time.Time, 1),
	}
	f.waiters = append(f.waiters, t)
	if d <= 0 {
		t.fired = true
		t.ch <- f.now
	}
	return t
}

// PendingTimers reports how many live (unfired, unstopped) timers exist.
func (f *Fake) PendingTimers() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	n := 0
	for _, t := range f.waiters {
		if !t.fired && !t.stopped {
			n++
		}
	}
	return n
}

// Advance moves the clock forward by d, firing every due timer (in wake-time
// order, ties broken by registration order) before returning. Timers created
// during the advance (e.g. by a goroutine woken by an earlier timer) whose
// wake time falls within the window fire as well.
func (f *Fake) Advance(d time.Duration) {
	if d <= 0 {
		return
	}
	f.mu.Lock()
	target := f.now.Add(d)

	for {
		t := f.nextDueLocked(target)
		if t == nil {
			break
		}
		t.fired = true
		f.now = t.wakeAt
		t.ch <- t.wakeAt
	}
	f.now = target
	f.pruneLocked()
	f.mu.Unlock()
}

func (t *fakeTimer) C() <-chan time.Time { return t.ch }

// Stop detaches the timer. It returns true only if it beat the firing.
func (t *fakeTimer) Stop() bool {
	f := t.fake
	f.mu.Lock()
	defer f.mu.Unlock()
	if t.fired || t.stopped {
		return false
	}
	t.stopped = true
	return true
}

// nextDueLocked returns the earliest live timer due by target, or nil.
// Caller must hold f.mu; sends are safe because timer channels are buffered
// (capacity 1), so a receiver that has gone away never blocks the advance.
func (f *Fake) nextDueLocked(target time.Time) *fakeTimer {
	var best *fakeTimer
	for _, t := range f.waiters {
		if t.fired || t.stopped || t.wakeAt.After(target) {
			continue
		}
		if best == nil || t.wakeAt.Before(best.wakeAt) ||
			(t.wakeAt.Equal(best.wakeAt) && t.seq < best.seq) {
			best = t
		}
	}
	return best
}

// pruneLocked drops timers that can never fire again and preserves
// registration order for the survivors.
func (f *Fake) pruneLocked() {
	kept := f.waiters[:0]
	for _, t := range f.waiters {
		if !t.fired && !t.stopped {
			kept = append(kept, t)
		}
	}
	f.waiters = kept
}
