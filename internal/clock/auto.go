package clock

import (
	"context"
	"sync"
	"time"
)

// Auto is a virtual clock that instantly fast-forwards by every requested
// sleep duration. It keeps multi-layer backoff tests fast and deterministic
// while preserving the observed delay sequence. Unlike Fake it needs no
// external driver; unlike Real it never blocks.
type Auto struct {
	mu  sync.Mutex
	now time.Time
}

// NewAuto returns an auto-advancing clock anchored at start.
func NewAuto(start time.Time) *Auto {
	if start.IsZero() {
		start = time.Unix(1_700_000_000, 0)
	}
	return &Auto{now: start}
}

func (a *Auto) Now() time.Time {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.now
}

func (a *Auto) Sleep(ctx context.Context, d time.Duration) bool {
	if err := ctx.Err(); err != nil {
		return false
	}
	a.mu.Lock()
	a.now = a.now.Add(d)
	a.mu.Unlock()
	return ctx.Err() == nil
}
