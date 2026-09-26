// Package clock provides a controllable time source so retry delays and
// deadlines can be tested deterministically.
package clock

import (
	"context"
	"sync"
	"time"
)

// Clock abstracts time. Sleep must return early with ctx.Err() when the
// context is cancelled or its deadline passes.
type Clock interface {
	Now() time.Time
	Sleep(ctx context.Context, d time.Duration) error
}

// Real is the production clock backed by the time package.
type Real struct{}

func (Real) Now() time.Time { return time.Now() }

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

// Fake is a manually advanced clock for tests. Sleepers block until Advance
// moves the clock past their wake time or their context is cancelled.
type Fake struct {
	mu      sync.Mutex
	now     time.Time
	waiters []waiter
}

type waiter struct {
	at time.Time
	ch chan struct{}
}

func NewFake(start time.Time) *Fake { return &Fake{now: start} }

func (f *Fake) Now() time.Time {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.now
}

// Waiters reports how many sleepers are currently registered. Tests use it
// to synchronize with a goroutine before advancing the clock.
func (f *Fake) Waiters() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.waiters)
}

func (f *Fake) Sleep(ctx context.Context, d time.Duration) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	f.mu.Lock()
	w := waiter{at: f.now.Add(d), ch: make(chan struct{})}
	f.waiters = append(f.waiters, w)
	f.mu.Unlock()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-w.ch:
		return nil
	}
}

// Advance moves the clock forward and wakes any sleepers whose wake time has
// been reached.
func (f *Fake) Advance(d time.Duration) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.now = f.now.Add(d)
	remaining := f.waiters[:0]
	for _, w := range f.waiters {
		if !w.at.After(f.now) {
			close(w.ch)
		} else {
			remaining = append(remaining, w)
		}
	}
	f.waiters = remaining
}
