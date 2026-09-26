// Package clock provides a controllable time source so streaming delays can
// be driven by real time in production and by a manually advanced fake clock
// in tests.
package clock

import (
	"context"
	"sync"
	"time"
)

// Clock abstracts time so tests can advance it deterministically.
type Clock interface {
	Now() time.Time
	// Sleep blocks until d has elapsed on this clock or ctx is cancelled.
	Sleep(ctx context.Context, d time.Duration) error
}

// Real is the wall-clock implementation.
type Real struct{}

func (Real) Now() time.Time { return time.Now() }

func (Real) Sleep(ctx context.Context, d time.Duration) error {
	if d <= 0 {
		return ctx.Err()
	}
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-t.C:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

type waiter struct {
	deadline time.Time
	ch       chan struct{}
}

// Fake is a manually advanced clock for deterministic tests.
type Fake struct {
	mu      sync.Mutex
	now     time.Time
	waiters []*waiter
}

func NewFake(start time.Time) *Fake { return &Fake{now: start} }

func (f *Fake) Now() time.Time {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.now
}

func (f *Fake) Sleep(ctx context.Context, d time.Duration) error {
	if d <= 0 {
		return ctx.Err()
	}
	w := &waiter{deadline: f.Now().Add(d), ch: make(chan struct{})}
	f.mu.Lock()
	f.waiters = append(f.waiters, w)
	f.mu.Unlock()
	select {
	case <-w.ch:
		return nil
	case <-ctx.Done():
		f.remove(w)
		return ctx.Err()
	}
}

// Waiters reports how many Sleep calls are currently blocked. Tests use it
// to synchronize with the sleeper before advancing the clock.
func (f *Fake) Waiters() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.waiters)
}

// Advance moves the fake clock forward and wakes every sleeper whose
// deadline has passed.
func (f *Fake) Advance(d time.Duration) {
	f.mu.Lock()
	f.now = f.now.Add(d)
	var keep []*waiter
	for _, w := range f.waiters {
		if !w.deadline.After(f.now) {
			close(w.ch)
		} else {
			keep = append(keep, w)
		}
	}
	f.waiters = keep
	f.mu.Unlock()
}

func (f *Fake) remove(target *waiter) {
	f.mu.Lock()
	defer f.mu.Unlock()
	for i, w := range f.waiters {
		if w == target {
			f.waiters = append(f.waiters[:i], f.waiters[i+1:]...)
			return
		}
	}
}
