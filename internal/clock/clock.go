// Package clock provides a controllable time source.
//
// The production wiring uses System; tests use Fake so that validator
// expiration and Last-Modified handling are fully deterministic. No code in
// this project should call time.Now directly — take a clock.Clock instead.
package clock

import "time"

// Clock is the minimal time surface the project depends on.
type Clock interface {
	Now() time.Time
}

// System returns the real wall clock.
type System struct{}

// Now implements Clock.
func (System) Now() time.Time { return time.Now().UTC() }

// Fake is a manually advanced clock. All methods return a fresh value rather
// than mutating shared state; the zero value is the UNIX epoch.
type Fake struct {
	instant time.Time
}

// NewFake returns a fake clock anchored at t (normalized to UTC).
func NewFake(t time.Time) Fake {
	return Fake{instant: t.UTC()}
}

// Now implements Clock.
func (f Fake) Now() time.Time { return f.instant }

// Advance returns a new Fake offset by d; the receiver is left unchanged.
func (f Fake) Advance(d time.Duration) Fake {
	return Fake{instant: f.instant.Add(d)}
}
