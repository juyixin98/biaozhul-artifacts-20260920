// Package clock provides an injectable time source.
//
// Production code uses Real (wall clock). Tests use Fake, whose time only
// advances when a test calls Advance, so lease expiry can be exercised
// deterministically without sleeping.
package clock

import (
	"sync"
	"time"
)

// Clock is the seam between the lock/resource components and wall time.
type Clock interface {
	Now() time.Time
}

// Real reads the system wall clock.
type Real struct{}

// Now implements Clock.
func (Real) Now() time.Time { return time.Now() }

// Fake is a concurrency-safe controllable clock.
type Fake struct {
	mu  sync.Mutex
	now time.Time
}

// NewFake returns a Fake anchored at start.
func NewFake(start time.Time) *Fake {
	return &Fake{now: start}
}

// Now implements Clock.
func (f *Fake) Now() time.Time {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.now
}

// Advance moves the clock forward by d.
func (f *Fake) Advance(d time.Duration) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.now = f.now.Add(d)
}
