// Package clock provides a clock abstraction so business-time logic
// (timestamps, processing-lease expiry) can be driven deterministically in
// tests while using wall-clock time in production-like runs.
package clock

import (
	"sync"
	"time"
)

// Clock is the time interface used by the idempotency layer.
type Clock interface {
	Now() time.Time
}

// Real reports wall-clock time.
type Real struct{}

// Now implements Clock.
func (Real) Now() time.Time { return time.Now().UTC() }

// Fake is a manually advanced clock. It is safe for concurrent use.
type Fake struct {
	mu sync.RWMutex
	t  time.Time
}

// NewFake returns a Fake clock set to t.
func NewFake(t time.Time) *Fake {
	return &Fake{t: t.UTC()}
}

// Now implements Clock.
func (f *Fake) Now() time.Time {
	f.mu.RLock()
	defer f.mu.RUnlock()
	return f.t
}

// Add moves the clock forward (or, rarely, backward) by d.
func (f *Fake) Add(d time.Duration) time.Time {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.t = f.t.Add(d)
	return f.t
}
