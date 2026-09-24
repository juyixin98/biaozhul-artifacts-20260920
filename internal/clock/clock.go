// Package clock provides the time abstraction used by the service so tests can
// drive decisions against a fully controlled clock.
package clock

import "time"

// Clock is the minimal time interface the service depends on.
type Clock interface {
	Now() time.Time
}

// System uses the real wall clock.
type System struct{}

// Now returns the current wall-clock time in UTC.
func (System) Now() time.Time { return time.Now().UTC() }

// Fake is a manually advanced clock.
type Fake struct{ T time.Time }

// NewFake builds a Fake clock at a fixed instant.
func NewFake(t time.Time) *Fake { return &Fake{T: t.UTC()} }

// Now returns the fake current time.
func (f *Fake) Now() time.Time { return f.T.UTC() }

// Advance moves the clock by d.
func (f *Fake) Advance(d time.Duration) { f.T = f.T.Add(d) }
