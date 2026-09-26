// Package clock provides a controllable time source so tests can
// advance time deterministically instead of sleeping.
package clock

import (
	"sync"
	"time"
)

// Clock is the time source used across the service.
type Clock interface {
	Now() time.Time
}

// Real is the production clock backed by time.Now.
type Real struct{}

// Now returns the current wall-clock time.
func (Real) Now() time.Time { return time.Now() }

// Fake is a manually advanced clock for tests.
type Fake struct {
	mu sync.Mutex
	t  time.Time
}

// NewFake returns a Fake clock pinned at start.
func NewFake(start time.Time) *Fake { return &Fake{t: start} }

// Now returns the fake current time.
func (f *Fake) Now() time.Time {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.t
}

// Advance moves the fake clock forward by d.
func (f *Fake) Advance(d time.Duration) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.t = f.t.Add(d)
}
