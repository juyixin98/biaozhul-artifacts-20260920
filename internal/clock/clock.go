// Package clock defines the schedulable time abstraction.
//
// Production code uses Real (wall/monotonic time); tests use Virtual, an
// explicitly advanced clock with deterministic timers. The scheduler depends
// only on the narrow interfaces in this package, which is what makes the
// whole limiter testable under virtual time with no sleeping.
package clock

import (
	"context"
	"errors"
	"sync"
	"time"
)

// Clock reports the current monotonic-ish instant. Implementations guarantee
// that Now never goes backwards.
type Clock interface {
	Now() time.Time
}

// TimerClock is a Clock that can schedule context-cancellable waits.
type TimerClock interface {
	Clock
	// Sleep blocks for d (or until ctx is done). It returns ctx.Err() on
	// cancellation/deadline, nil after the full duration elapsed.
	Sleep(ctx context.Context, d time.Duration) error
}

// Real is the production clock backed by the Go runtime monotonic clock.
type Real struct{}

// Now implements Clock.
func (Real) Now() time.Time { return time.Now() }

// Sleep implements TimerClock.
func (Real) Sleep(ctx context.Context, d time.Duration) error {
	if d <= 0 {
		return ctx.Err()
	}
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-t.C:
		return nil
	}
}

// Virtual is a manually advanced clock.
//
// Time only moves when Advance is called, making refill behavior exactly
// predictable in tests. Timers scheduled via Sleep fire when the clock is
// advanced to (or past) their deadline; all goroutines that become runnable
// from a single Advance are woken together, which is what concurrency tests
// use to release a batch of waiters at the same virtual instant.
type Virtual struct {
	mu      sync.Mutex
	cond    *sync.Cond
	now     time.Time
	timers  []*virtualTimer
	nextID  uint64
	waiters int
}

type virtualTimer struct {
	id       uint64
	deadline time.Time
	fired    bool
	canceled bool
}

// NewVirtual creates a virtual clock starting at t (a zero time is fine; only
// elapsed differences matter).
func NewVirtual(t time.Time) *Virtual {
	v := &Virtual{now: t}
	v.cond = sync.NewCond(&v.mu)
	return v
}

// Now implements Clock.
func (v *Virtual) Now() time.Time {
	v.mu.Lock()
	defer v.mu.Unlock()
	return v.now
}

// Waiters reports how many goroutines are currently blocked in Sleep.
func (v *Virtual) Waiters() int {
	v.mu.Lock()
	defer v.mu.Unlock()
	return v.waiters
}

// WaitForWaiters blocks until at least n goroutines are parked in Sleep.
// Tests call it before Advance so they know a batch is truly waiting.
func (v *Virtual) WaitForWaiters(n int) {
	v.mu.Lock()
	defer v.mu.Unlock()
	for v.waiters < n {
		v.cond.Wait()
	}
}

// Sleep implements TimerClock on virtual time. A timer fires the first time
// the clock reaches its deadline.
func (v *Virtual) Sleep(ctx context.Context, d time.Duration) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	if d <= 0 {
		return nil
	}
	v.mu.Lock()
	v.nextID++
	t := &virtualTimer{id: v.nextID, deadline: v.now.Add(d)}
	v.timers = append(v.timers, t)
	v.waiters++
	v.cond.Broadcast()

	// Cancellation wakes the cond wait; the loop then observes ctx.Err and
	// the timer is marked so Advance can drop it from the pending list.
	stopAfter := context.AfterFunc(ctx, func() {
		v.mu.Lock()
		t.canceled = true
		v.cond.Broadcast()
		v.mu.Unlock()
	})
	defer stopAfter()

	for !t.fired && ctx.Err() == nil {
		v.cond.Wait()
	}
	fired := t.fired
	v.waiters--
	v.mu.Unlock()

	if !fired {
		return ctx.Err()
	}
	return nil
}

// Advance moves the clock forward by d (d must be >= 0) and wakes every
// timer whose deadline is reached. Timers that have fired are compacted out
// of the pending list afterwards.
func (v *Virtual) Advance(d time.Duration) {
	if d < 0 {
		panic(errors.New("clock: cannot advance virtual clock backwards"))
	}
	if d == 0 {
		return
	}
	v.mu.Lock()
	v.now = v.now.Add(d)
	kept := v.timers[:0]
	for _, t := range v.timers {
		if !t.fired && !t.deadline.After(v.now) {
			t.fired = true
		}
		if !t.fired && !t.canceled {
			kept = append(kept, t)
		}
	}
	v.timers = kept
	v.mu.Unlock()
	v.cond.Broadcast()
}
