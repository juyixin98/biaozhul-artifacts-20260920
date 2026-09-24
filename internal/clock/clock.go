// Package clock provides a controllable time source.
//
// In manual mode the clock only moves when Set/Advance is called — this is
// what makes deterministic, event-time driven tests possible. In real mode
// Now() follows wall-clock time.
package clock

import (
	"sync"
	"time"
)

type Clock struct {
	mu   sync.RWMutex
	real bool
	now  time.Time
}

// NewManual creates a clock fixed at initial.
func NewManual(initial time.Time) *Clock {
	return &Clock{real: false, now: initial}
}

// NewReal creates a clock that follows the wall clock.
func NewReal() *Clock {
	return &Clock{real: true}
}

// IsManual reports whether the clock is controllable.
func (c *Clock) IsManual() bool {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return !c.real
}

// Now returns the current time.
func (c *Clock) Now() time.Time {
	c.mu.RLock()
	defer c.mu.RUnlock()
	if c.real {
		return time.Now().UTC()
	}
	return c.now
}

// Set fixes the clock to t (UTC). Only valid in manual mode.
func (c *Clock) Set(t time.Time) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.real {
		return ErrRealClock
	}
	c.now = t.UTC()
	return nil
}

// Advance moves a manual clock forward and returns the new time.
func (c *Clock) Advance(d time.Duration) (time.Time, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.real {
		return time.Time{}, ErrRealClock
	}
	if d < 0 {
		return time.Time{}, ErrNegativeAdvance
	}
	c.now = c.now.Add(d)
	return c.now, nil
}
