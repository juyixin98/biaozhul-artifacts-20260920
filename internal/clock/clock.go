// Package clock provides the time abstraction used by the controllers so the
// renewal window, clock-skew tolerance and requeue calculations can be
// exercised deterministically in tests.
package clock

import "time"

// Clock returns the current time.
type Clock interface {
	Now() time.Time
}

// System is the wall-clock implementation used by the manager.
type System struct{}

// Now returns time.Now().
func (System) Now() time.Time { return time.Now() }

// Fake is a controllable clock for tests.
type Fake struct{ T time.Time }

// Now returns the frozen/advanced time.
func (f *Fake) Now() time.Time { return f.T }

// Set moves the clock.
func (f *Fake) Set(t time.Time) { f.T = t }

// Add moves the clock forward.
func (f *Fake) Add(d time.Duration) { f.T = f.T.Add(d) }
