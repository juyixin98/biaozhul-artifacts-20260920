// Package clock provides a controllable time source.
//
// Production code uses RealClock while tests use FakeClock, which only
// advances when Advance is called. This keeps retry backoff and HTTP-date
// precondition checks fully deterministic.
package clock

import (
	"context"
	"sync"
	"time"
)

// Clock is the minimal time surface the rest of the codebase depends on.
type Clock interface {
	Now() time.Time
	// Sleep blocks for d or until ctx is canceled. Non-positive d returns
	// immediately.
	Sleep(ctx context.Context, d time.Duration) error
}

// RealClock delegates to the wall clock.
type RealClock struct{}

func (RealClock) Now() time.Time { return time.Now() }

func (RealClock) Sleep(ctx context.Context, d time.Duration) error {
	if d <= 0 {
		return nil
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

type waiter struct {
	until time.Time
	done  chan struct{}
}

// FakeClock is a manually controlled clock. Sleepers are released when
// Advance moves the fake time to (or past) their wake-up time.
type FakeClock struct {
	mu      sync.Mutex
	now     time.Time
	waiters []*waiter
}

// NewFake returns a FakeClock set to start.
func NewFake(start time.Time) *FakeClock {
	return &FakeClock{now: start.UTC()}
}

func (c *FakeClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

// WaiterCount reports how many goroutines are currently blocked in Sleep.
func (c *FakeClock) WaiterCount() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return len(c.waiters)
}

func (c *FakeClock) Sleep(ctx context.Context, d time.Duration) error {
	if d <= 0 {
		return nil
	}
	c.mu.Lock()
	w := &waiter{until: c.now.Add(d), done: make(chan struct{})}
	c.waiters = append(c.waiters, w)
	c.mu.Unlock()

	select {
	case <-ctx.Done():
		c.remove(w)
		return ctx.Err()
	case <-w.done:
		return nil
	}
}

func (c *FakeClock) remove(w *waiter) {
	c.mu.Lock()
	defer c.mu.Unlock()
	for i, x := range c.waiters {
		if x == w {
			c.waiters = append(c.waiters[:i], c.waiters[i+1:]...)
			return
		}
	}
}

// Advance moves the clock forward by d and releases every waiter whose
// deadline has been reached. It must not be called with a negative duration.
func (c *FakeClock) Advance(d time.Duration) {
	if d <= 0 {
		return
	}
	c.mu.Lock()
	c.now = c.now.Add(d)
	due := make([]*waiter, 0)
	kept := c.waiters[:0]
	for _, w := range c.waiters {
		if !w.until.After(c.now) {
			due = append(due, w)
		} else {
			kept = append(kept, w)
		}
	}
	c.waiters = kept
	c.mu.Unlock()

	for _, w := range due {
		close(w.done)
	}
}
