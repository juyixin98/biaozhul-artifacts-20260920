// Package clock provides an injectable time source.
//
// All lifecycle decisions (expiry windows, transfer timeouts) go through a
// Clock. Production wires SystemClock; tests pass a frozen ManualClock and
// advance it manually, so a "5 day transfer wait" needs no real waiting.
package clock

import "time"

type Clock interface {
	Now() time.Time
}

// SystemClock returns the real wall-clock time.
type SystemClock struct{}

func (SystemClock) Now() time.Time { return time.Now().UTC() }

// ManualClock is a frozen, externally advanced clock for tests.
type ManualClock struct {
	t time.Time
}

func NewManual(t time.Time) *ManualClock { return &ManualClock{t: t.UTC()} }

func (c *ManualClock) Now() time.Time { return c.t }

// Advance moves the clock forward.
func (c *ManualClock) Advance(d time.Duration) time.Time {
	c.t = c.t.Add(d)
	return c.t
}

// Set jumps the clock to an absolute instant.
func (c *ManualClock) Set(t time.Time) { c.t = t.UTC() }
