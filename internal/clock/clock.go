// Package clock provides a controllable time source so tests and
// fault-injection paths never depend on the wall clock.
package clock

import (
	"sync"
	"time"
)

// Clock abstracts time. Implementations must be safe for concurrent use.
type Clock interface {
	Now() time.Time
	Sleep(d time.Duration)
}

// Real is the production clock backed by the time package.
type Real struct{}

func (Real) Now() time.Time        { return time.Now() }
func (Real) Sleep(d time.Duration) { time.Sleep(d) }

// Fake is a manually advanced clock for tests and deterministic runs.
type Fake struct {
	mu  sync.Mutex
	now time.Time
}

// NewFake returns a Fake clock pinned at start.
func NewFake(start time.Time) *Fake { return &Fake{now: start} }

func (f *Fake) Now() time.Time {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.now
}

// Sleep advances the fake clock without blocking.
func (f *Fake) Sleep(d time.Duration) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.now = f.now.Add(d)
}

// Advance moves the fake clock forward.
func (f *Fake) Advance(d time.Duration) { f.Sleep(d) }
