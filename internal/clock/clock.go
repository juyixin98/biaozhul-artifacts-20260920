// Package clock provides a controllable time source so tests and the
// fault-injection client can drive time deterministically.
package clock

import (
	"sync"
	"time"
)

// Clock is a source of the current time.
type Clock interface {
	Now() time.Time
}

// Real is the wall-clock implementation.
type Real struct{}

// Now returns the current wall-clock time in UTC.
func (Real) Now() time.Time { return time.Now().UTC() }

// Fake is a manually controlled clock. It never advances on its own.
type Fake struct {
	mu sync.Mutex
	t  time.Time
}

// NewFake returns a Fake clock pinned at t.
func NewFake(t time.Time) *Fake { return &Fake{t: t.UTC()} }

// Now returns the clock's current (frozen) time.
func (f *Fake) Now() time.Time {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.t
}

// Set pins the clock to t.
func (f *Fake) Set(t time.Time) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.t = t.UTC()
}

// Advance moves the clock forward by d.
func (f *Fake) Advance(d time.Duration) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.t = f.t.Add(d)
}
