// Package clock provides a controllable clock so retry/backoff logic never
// depends on wall-clock sleeps in tests.
package clock

import (
	"context"
	"sync"
	"time"
)

// Clock is the minimal time surface used by the retry package.
type Clock interface {
	Now() time.Time
	// Sleep blocks for d or until ctx is done. It reports false when the
	// context expired before the delay elapsed.
	Sleep(ctx context.Context, d time.Duration) bool
}

// Real is a wall-clock implementation suitable for the demo server.
type Real struct{}

func (Real) Now() time.Time { return time.Now() }

func (Real) Sleep(ctx context.Context, d time.Duration) bool {
	if d <= 0 {
		return ctx.Err() == nil
	}
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-t.C:
		return true
	}
}

// Fake is a manually advanced clock. Sleepers are released by Advance.
type Fake struct {
	mu      sync.Mutex
	now     time.Time
	pending []*timer
	seq     int64
}

type timer struct {
	at  time.Time
	ch  chan struct{}
	seq int64
}

// NewFake returns a fake clock anchored at a fixed instant.
func NewFake(start time.Time) *Fake {
	if start.IsZero() {
		start = time.Unix(1_700_000_000, 0)
	}
	return &Fake{now: start}
}

func (f *Fake) Now() time.Time {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.now
}

// Sleep registers a timer that fires when the clock is advanced to/over now+d.
func (f *Fake) Sleep(ctx context.Context, d time.Duration) bool {
	if d <= 0 {
		return ctx.Err() == nil
	}
	f.mu.Lock()
	f.seq++
	t := &timer{at: f.now.Add(d), ch: make(chan struct{}), seq: f.seq}
	f.pending = append(f.pending, t)
	f.mu.Unlock()

	select {
	case <-ctx.Done():
		f.remove(t)
		return false
	case <-t.ch:
		return true
	}
}

func (f *Fake) remove(t *timer) {
	f.mu.Lock()
	defer f.mu.Unlock()
	for i, p := range f.pending {
		if p == t {
			f.pending = append(f.pending[:i], f.pending[i+1:]...)
			return
		}
	}
}

// Advance moves the clock by d, firing every timer whose deadline is reached.
// Due timers fire sequentially in deadline order; each must be received
// before the next is signalled, matching real timer semantics closely enough
// for deterministic backoff tests.
func (f *Fake) Advance(d time.Duration) {
	f.mu.Lock()
	f.now = f.now.Add(d)
	due := f.takeDueLocked()
	f.mu.Unlock()
	for _, t := range due {
		close(t.ch)
	}
}

func (f *Fake) takeDueLocked() []*timer {
	var due []*timer
	rest := f.pending[:0]
	for _, t := range f.pending {
		if !t.at.After(f.now) {
			due = append(due, t)
		} else {
			rest = append(rest, t)
		}
	}
	f.pending = rest
	// Stable order by deadline, ties preserve insertion order.
	for i := 1; i < len(due); i++ {
		for j := i; j > 0 && due[j-1].at.After(due[j].at); j-- {
			due[j-1], due[j] = due[j], due[j-1]
		}
	}
	return due
}

// Pending reports how many sleeps are currently registered.
func (f *Fake) Pending() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.pending)
}
