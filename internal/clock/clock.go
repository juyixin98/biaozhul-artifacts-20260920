// Package clock provides an injectable time source so tests (and the
// admin debug endpoint) can drive a deterministic virtual clock while the
// production server uses wall-clock time.
package clock

import (
	"sync"
	"time"
)

// Clock is the time source used everywhere in the service.
type Clock interface {
	Now() time.Time
}

// Real is wall-clock UTC time.
type Real struct{}

// Now returns the current wall-clock time in UTC.
func (Real) Now() time.Time { return time.Now().UTC() }

// Virtual is a manually advanced clock. Advance moves it forward instantly,
// which is what makes stale-window tests fast and deterministic: advancing
// the virtual clock does not wait real time.
type Virtual struct {
	mu  sync.RWMutex
	now time.Time
}

// NewVirtual creates a virtual clock anchored at t.
func NewVirtual(t time.Time) *Virtual {
	return &Virtual{now: t.UTC()}
}

// Now returns the virtual instant.
func (v *Virtual) Now() time.Time {
	v.mu.RLock()
	defer v.mu.RUnlock()
	return v.now
}

// Advance moves the virtual clock forward by d (negative durations are
// ignored — clock rewinds of the *server* clock are not a feature).
func (v *Virtual) Advance(d time.Duration) time.Time {
	v.mu.Lock()
	defer v.mu.Unlock()
	if d > 0 {
		v.now = v.now.Add(d)
	}
	return v.now
}

// Set jumps the clock to an absolute instant (must not be before current).
func (v *Virtual) Set(t time.Time) time.Time {
	v.mu.Lock()
	defer v.mu.Unlock()
	t = t.UTC()
	if t.After(v.now) {
		v.now = t
	}
	return v.now
}
