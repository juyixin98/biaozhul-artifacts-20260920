// Package vclock provides a manually controlled clock for deterministic tests.
//
// The virtual clock only advances when Advance/Set are called. Timers registered
// with After fire when the clock is advanced to (or past) their deadline. Real
// goroutines park on the returned timer channels and are woken by Advance, so a
// whole distributed-style interaction can be driven with zero real-time waits.
package vclock

import (
	"context"
	"sort"
	"sync"
	"time"
)

// Clock is the time source used by the circuit breaker.
type Clock interface {
	Now() time.Time
}

// SystemClock is a Clock backed by wall-clock time.
type SystemClock struct{}

// Now returns the current wall-clock time.
func (SystemClock) Now() time.Time { return time.Now() }

// Timer is a timer registered against a VirtualClock.
type Timer struct {
	deadline time.Time
	seq      uint64
	fired    bool
	ch       chan time.Time
	vc       *VirtualClock
}

// C returns the channel that receives the virtual time when the timer fires.
func (t *Timer) C() <-chan time.Time { return t.ch }

// Stop removes the timer from the clock. It reports whether the timer was
// still pending (false if it had already fired).
func (t *Timer) Stop() bool { return t.vc.remove(t) }

// VirtualClock is a clock whose time only moves on demand.
type VirtualClock struct {
	mu      sync.Mutex
	now     time.Time
	nextSeq uint64
	timers  []*Timer
}

// NewVirtual creates a VirtualClock. A zero start is replaced with a fixed
// deterministic epoch so timestamps are easy to read in reports.
func NewVirtual(start time.Time) *VirtualClock {
	if start.IsZero() {
		start = time.UnixMilli(0).UTC()
	}
	return &VirtualClock{now: start}
}

// Now returns the current virtual time.
func (vc *VirtualClock) Now() time.Time {
	vc.mu.Lock()
	defer vc.mu.Unlock()
	return vc.now
}

// Set moves the clock to t. Pending timers are not evaluated; call Advance to
// fire due timers.
func (vc *VirtualClock) Set(t time.Time) {
	vc.mu.Lock()
	defer vc.mu.Unlock()
	vc.now = t
}

// After registers a timer that fires after d virtual time. If d has already
// elapsed the timer fires immediately.
func (vc *VirtualClock) After(d time.Duration) *Timer {
	vc.mu.Lock()
	defer vc.mu.Unlock()

	t := &Timer{
		deadline: vc.now.Add(d),
		seq:      vc.nextSeq,
		ch:       make(chan time.Time, 1),
		vc:       vc,
	}
	vc.nextSeq++

	if !t.deadline.After(vc.now) {
		t.fired = true
		t.ch <- vc.now
		return t
	}
	vc.timers = append(vc.timers, t)
	return t
}

func (vc *VirtualClock) remove(target *Timer) bool {
	vc.mu.Lock()
	defer vc.mu.Unlock()
	for i, t := range vc.timers {
		if t == target {
			vc.timers = append(vc.timers[:i], vc.timers[i+1:]...)
			return true
		}
	}
	return false
}

// Advance moves the clock forward by d and fires every timer due at or before
// the new virtual time, in deadline order. The clock is moved to the target time
// before any timer is delivered, so goroutines unblocked by the firing always
// observe the new time via Now. It returns the number of timers fired.
//
// The set of due timers is computed once, up front: a timer registered by an
// unblocked goroutine during this Advance (even one already due) fires on a
// later Advance, not this one. Goroutines unblocked by the firing run
// concurrently; callers that need their side effects to have completed must
// synchronize at the application boundary.
func (vc *VirtualClock) Advance(d time.Duration) int {
	vc.mu.Lock()
	target := vc.now.Add(d)

	var due []*Timer
	for _, t := range vc.timers {
		if !t.fired && !t.deadline.After(target) {
			due = append(due, t)
		}
	}
	sort.Slice(due, func(i, j int) bool {
		if due[i].deadline.Equal(due[j].deadline) {
			return due[i].seq < due[j].seq
		}
		return due[i].deadline.Before(due[j].deadline)
	})

	dueSet := make(map[*Timer]bool, len(due))
	for _, t := range due {
		dueSet[t] = true
		t.fired = true
	}
	if len(due) > 0 {
		remaining := vc.timers[:0]
		for _, t := range vc.timers {
			if !dueSet[t] {
				remaining = append(remaining, t)
			}
		}
		vc.timers = remaining
	}

	// Move time forward before delivering events: an unblocked goroutine
	// reading Now() must see the target time.
	vc.now = target
	vc.mu.Unlock()

	fired := 0
	for _, t := range due {
		select {
		case t.ch <- t.deadline:
		default:
		}
		fired++
	}
	return fired
}

// Pending reports how many registered timers have not fired.
func (vc *VirtualClock) Pending() int {
	vc.mu.Lock()
	defer vc.mu.Unlock()
	return len(vc.timers)
}

// ContextWithTimeout derives a context whose deadline is measured against the
// virtual clock rather than wall-clock time. It behaves like
// context.WithTimeout: the returned context is canceled with
// context.DeadlineExceeded when virtual time reaches the deadline, with the
// parent's error when the parent is canceled, or with context.Canceled when
// the returned cancel function is called.
func ContextWithTimeout(parent context.Context, vc *VirtualClock, d time.Duration) (context.Context, context.CancelFunc) {
	c := &virtualDeadlineCtx{
		parent:   parent,
		done:     make(chan struct{}),
		deadline: vc.Now().Add(d),
	}
	if err := parent.Err(); err != nil {
		c.err = err
		close(c.done)
		return c, func() {}
	}

	stop := make(chan struct{})
	var stopOnce sync.Once
	closeStop := func() { stopOnce.Do(func() { close(stop) }) }

	timer := vc.After(d)
	go func() {
		select {
		case <-timer.C():
			c.cancel(context.DeadlineExceeded)
		case <-parent.Done():
			timer.Stop()
			c.cancel(parent.Err())
		case <-stop:
			timer.Stop()
		}
	}()

	cancel := func() {
		closeStop()
		c.cancel(context.Canceled)
	}
	return c, cancel
}

type virtualDeadlineCtx struct {
	parent   context.Context
	deadline time.Time

	mu   sync.Mutex
	err  error
	done chan struct{}
}

func (c *virtualDeadlineCtx) Deadline() (time.Time, bool) { return c.deadline, true }

func (c *virtualDeadlineCtx) Done() <-chan struct{} { return c.done }

func (c *virtualDeadlineCtx) Err() error {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.err
}

func (c *virtualDeadlineCtx) Value(key any) any { return c.parent.Value(key) }

func (c *virtualDeadlineCtx) cancel(err error) {
	if err == nil {
		err = context.Canceled
	}
	c.mu.Lock()
	if c.err != nil {
		c.mu.Unlock()
		return
	}
	c.err = err
	close(c.done)
	c.mu.Unlock()
}
