// Package clock provides a controllable time source.
//
// Production code uses Real; tests and deterministic verification runs use
// Fake, which only advances when code sleeps or a test calls Advance. This
// makes timestamps and time-based behavior fully reproducible.
package clock

import (
	"sync"
	"time"
)

// Clock is the time abstraction used by the rest of the project.
type Clock interface {
	Now() time.Time
	Since(t time.Time) time.Duration
	Sleep(d time.Duration)
}

// Real is a Clock backed by the wall clock.
type Real struct{}

func (Real) Now() time.Time                  { return time.Now() }
func (Real) Since(t time.Time) time.Duration { return time.Since(t) }
func (Real) Sleep(d time.Duration)           { time.Sleep(d) }

// Fake is a manually controlled Clock. Sleep does not block; it advances the
// fake instant by the requested duration, so retry/backoff loops run instantly
// while still observing elapsed time.
type Fake struct {
	mu  sync.Mutex
	now time.Time
}

func NewFake(start time.Time) *Fake {
	return &Fake{now: start}
}

func (f *Fake) Now() time.Time {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.now
}

func (f *Fake) Since(t time.Time) time.Duration {
	return f.Now().Sub(t)
}

// Sleep advances fake time instead of blocking the goroutine.
func (f *Fake) Sleep(d time.Duration) {
	f.Advance(d)
}

// Advance moves the fake clock forward and returns the new instant.
func (f *Fake) Advance(d time.Duration) time.Time {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.now = f.now.Add(d)
	return f.now
}
