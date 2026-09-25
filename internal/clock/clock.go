package clock

import (
	"sync"
	"time"
)

// Clock is the seam that makes timeout behaviour deterministic in tests.
// Assembler code must obtain "now" only through this interface (never
// time.Now directly), so tests can advance a fake clock instead of sleeping.
type Clock interface {
	Now() time.Time
}

// System uses the wall clock. Used by the real server binary.
type System struct{}

func (System) Now() time.Time { return time.Now() }

// Fake is a manually advanced clock. It is safe for concurrent use.
type Fake struct {
	mu sync.Mutex
	t  time.Time
}

func NewFake(start time.Time) *Fake {
	return &Fake{t: start}
}

func (f *Fake) Now() time.Time {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.t
}

// Advance moves the clock forward and returns the new time.
func (f *Fake) Advance(d time.Duration) time.Time {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.t = f.t.Add(d)
	return f.t
}
