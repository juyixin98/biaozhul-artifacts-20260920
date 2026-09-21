package domain

import "time"

// Clock is injected so tests can pin or advance time.
type Clock interface {
	Now() time.Time
}

type RealClock struct{}

func (RealClock) Now() time.Time { return time.Now().UTC() }

type FixedClock struct{ T time.Time }

func (c FixedClock) Now() time.Time { return c.T }
func (c *FixedClock) Advance(d time.Duration) time.Time {
	c.T = c.T.Add(d)
	return c.T
}
