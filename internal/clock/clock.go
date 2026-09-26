// Package clock provides a controllable virtual clock plus a real-clock
// implementation. The breaker, the client and the in-process fake upstream all
// read time through this interface so that tests can deterministically
// reproduce timing-dependent races (late failures, half-open probe windows).
package clock

import (
	"sync"
	"time"
)

// Clock is the time source used by every component in the project.
type Clock interface {
	Now() time.Time
	// Sleep blocks for d. For the virtual clock it blocks until the clock is
	// advanced by at least d (callers waiting longer are released first).
	Sleep(d time.Duration)
	// After is the channel equivalent of Sleep.
	After(d time.Duration) <-chan time.Time
}

// Real is a Clock backed by the wall clock.
type Real struct{}

func NewReal() *Real { return &Real{} }

func (Real) Now() time.Time                         { return time.Now() }
func (Real) Sleep(d time.Duration)                  { time.Sleep(d) }
func (Real) After(d time.Duration) <-chan time.Time { return time.After(d) }

// waiters are ordered by target time so advancing can release the due ones.
type waiter struct {
	at time.Time
	ch chan time.Time
}

// Virtual is a manually-advanced clock. Nothing fires on its own: time only
// moves when Advance (or AdvanceUntil) is called, which makes every
// time-dependent scenario fully reproducible.
type Virtual struct {
	mu       sync.Mutex
	now      time.Time
	sleepers []waiter // sorted ascending by at
}

// NewVirtual returns a virtual clock anchored at a fixed, recognisable instant.
func NewVirtual() *Virtual {
	return &Virtual{now: time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)}
}

func (c *Virtual) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

func (c *Virtual) After(d time.Duration) <-chan time.Time {
	c.mu.Lock()
	w := waiter{at: c.now.Add(d), ch: make(chan time.Time, 1)}
	c.insertLocked(w)
	c.mu.Unlock()
	return w.ch
}

func (c *Virtual) Sleep(d time.Duration) {
	<-c.After(d)
}

func (c *Virtual) insertLocked(w waiter) {
	i := 0
	for i < len(c.sleepers) && c.sleepers[i].at.Before(w.at) {
		i++
	}
	c.sleepers = append(c.sleepers, waiter{})
	copy(c.sleepers[i+1:], c.sleepers[i:])
	c.sleepers[i] = w
}

// Advance moves the clock by d and releases every sleeper whose deadline is
// due, firing them at their own deadline time. Sleepers are released while the
// lock is NOT held so a fired goroutine may immediately register a new timer.
func (c *Virtual) Advance(d time.Duration) {
	if d <= 0 {
		return
	}
	c.mu.Lock()
	target := c.now.Add(d)
	due := c.takeDueLocked(target)
	c.now = target
	c.mu.Unlock()

	fireDueWaiters(due)
}

// AdvanceUntil moves the clock exactly to t (no-op if t is in the past).
func (c *Virtual) AdvanceUntil(t time.Time) {
	c.mu.Lock()
	if !t.After(c.now) {
		c.mu.Unlock()
		return
	}
	due := c.takeDueLocked(t)
	c.now = t
	c.mu.Unlock()

	fireDueWaiters(due)
}

// takeDueLocked removes and returns all sleepers due at or before target.
func (c *Virtual) takeDueLocked(target time.Time) []waiter {
	n := 0
	for n < len(c.sleepers) && !c.sleepers[n].at.After(target) {
		n++
	}
	due := c.sleepers[:n]
	// Copy out: the backing array stays owned by c.sleepers after reslice.
	out := append([]waiter(nil), due...)
	c.sleepers = c.sleepers[n:]
	return out
}

// HasPendingTimers reports whether any goroutine is blocked in Sleep/After.
func (c *Virtual) HasPendingTimers() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return len(c.sleepers) > 0
}

// fireDueWaiters delivers to buffered channels; delivery order is shortest
// deadline first so equal-step sleeps behave like real time.
func fireDueWaiters(due []waiter) {
	for _, w := range due {
		w.ch <- w.at
	}
}
