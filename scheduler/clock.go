package scheduler

import (
	"context"
	"sync"
	"time"
)

// RealClock uses wall-clock time and timer-based sleeping.
type RealClock struct{}

func (RealClock) Now() time.Time { return time.Now() }

func (RealClock) Sleep(ctx DoneContext, d time.Duration) bool {
	if d <= 0 {
		return ctx == nil || ctx.Err() == nil
	}
	timer := time.NewTimer(d)
	defer timer.Stop()
	if ctx == nil {
		<-timer.C
		return true
	}
	select {
	case <-timer.C:
		return true
	case <-ctx.Done():
		return false
	}
}

// fakeWaiter is one blocked FakeClock.Sleep call.
type fakeWaiter struct {
	deadline time.Time
	// wake is closed exactly once: by expiry or by the cancel watcher.
	wake chan struct{}
	// fired is set (under FakeClock.mu) when closed due to time expiry,
	// so a cancel/expiry race is resolved in favor of the timer.
	fired bool
}

// FakeClock is a controllable clock for deterministic tests:
// Sleep registers a waiter that completes when the clock is advanced
// past its deadline, or when its context is canceled.
//
// All bookkeeping is done under FakeClock.mu; no user callback (executor,
// sink, scheduler) is ever invoked while that lock is held.
type FakeClock struct {
	mu      sync.Mutex
	now     time.Time
	waiters []*fakeWaiter
}

// NewFakeClock returns a FakeClock anchored at t (or the Unix epoch when zero).
func NewFakeClock(t time.Time) *FakeClock {
	if t.IsZero() {
		t = time.Unix(0, 0)
	}
	return &FakeClock{now: t}
}

func (c *FakeClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

// Sleep blocks until Advance covers d or ctx is canceled.
// Returns true if the full delay elapsed, false if canceled.
func (c *FakeClock) Sleep(ctx DoneContext, d time.Duration) bool {
	if d <= 0 {
		return ctx == nil || ctx.Err() == nil
	}
	c.mu.Lock()
	w := &fakeWaiter{deadline: c.now.Add(d), wake: make(chan struct{})}
	c.waiters = append(c.waiters, w)
	var cancelCh <-chan struct{}
	if ctx != nil {
		cancelCh = ctx.Done()
	}
	c.mu.Unlock()

	// Cancel watcher: its only job is to close the local wake channel.
	// It acquires the clock lock briefly and never touches caller state,
	// so it cannot participate in an engine-level lock cycle.
	stopWatch := make(chan struct{})
	defer close(stopWatch)
	if cancelCh != nil {
		go func() {
			select {
			case <-cancelCh:
				c.mu.Lock()
				expiredByTimer := w.fired
				if !expiredByTimer {
					c.removeWaiterLocked(w)
				}
				c.mu.Unlock()
				if !expiredByTimer {
					close(w.wake)
				}
			case <-stopWatch:
			}
		}()
	}

	<-w.wake
	c.mu.Lock()
	outcome := w.fired
	c.mu.Unlock()
	return outcome
}

// Advance moves the clock by d, closing the wake channel of every waiter
// whose deadline has now passed. Nothing user-facing runs under the lock.
func (c *FakeClock) Advance(d time.Duration) {
	if d <= 0 {
		return
	}
	c.mu.Lock()
	c.now = c.now.Add(d)
	fired := c.takeExpiredLocked()
	c.mu.Unlock()
	for _, w := range fired {
		w.fired = true
		close(w.wake)
	}
}

// PeekWaiters reports how many Sleep calls are currently blocked.
func (c *FakeClock) PeekWaiters() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return len(c.waiters)
}

func (c *FakeClock) takeExpiredLocked() []*fakeWaiter {
	var fired []*fakeWaiter
	remaining := c.waiters[:0]
	for _, w := range c.waiters {
		if !w.deadline.After(c.now) {
			fired = append(fired, w)
		} else {
			remaining = append(remaining, w)
		}
	}
	c.waiters = remaining
	return fired
}

func (c *FakeClock) removeWaiterLocked(target *fakeWaiter) {
	out := c.waiters[:0]
	for _, w := range c.waiters {
		if w != target {
			out = append(out, w)
		}
	}
	c.waiters = out
}

// AsDoneContext wraps a standard context as the scheduler's DoneContext.
func AsDoneContext(ctx context.Context) DoneContext { return ctxAdapter{ctx} }

type ctxAdapter struct{ ctx context.Context }

func (a ctxAdapter) Done() <-chan struct{} { return a.ctx.Done() }
func (a ctxAdapter) Err() error            { return a.ctx.Err() }
