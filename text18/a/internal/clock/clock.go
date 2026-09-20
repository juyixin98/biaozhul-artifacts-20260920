// Package clock provides a replaceable time source.
package clock

import "time"

type Clock interface {
	Now() time.Time
}

type Real struct{}

func (Real) Now() time.Time { return time.Now().UTC() }

// Mock is a manually controlled clock used in tests.
type Mock struct{ T time.Time }

func (m *Mock) Now() time.Time {
	if m.T.IsZero() {
		return time.Now().UTC()
	}
	return m.T
}

func (m *Mock) Advance(d time.Duration) { m.T = m.T.Add(d) }
