// Package clock abstracts wall-clock access so replay boundaries and key
// intervals can be tested deterministically.
package clock

import "time"

// Clock returns "now" in UTC.
type Clock interface {
	Now() time.Time
}

// System is the production clock.
type System struct{}

func (System) Now() time.Time { return time.Now().UTC() }

// Mock is a manually controlled clock for tests.
type Mock struct{ T time.Time }

func NewMock(t time.Time) *Mock { return &Mock{T: t.UTC()} }
func (m *Mock) Now() time.Time  { return m.T.UTC() }

// Advance moves the mock clock forward.
func (m *Mock) Advance(d time.Duration) { m.T = m.T.Add(d) }

// Set jumps the mock clock to an absolute instant.
func (m *Mock) Set(t time.Time) { m.T = t.UTC() }
