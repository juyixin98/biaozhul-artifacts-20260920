package clock

import (
	"context"
	"time"
)

// RealClock is the production Clock backed by the wall clock.
type RealClock struct{}

// NewRealClock returns the wall-clock implementation.
func NewRealClock() RealClock { return RealClock{} }

func (RealClock) Now() time.Time { return time.Now() }

func (RealClock) NewTimer(d time.Duration) Timer {
	return &wallTimer{t: time.NewTimer(d)}
}

func (RealClock) After(d time.Duration) <-chan time.Time { return time.After(d) }

func (c RealClock) Sleep(ctx context.Context, d time.Duration) error {
	return sleepOn(ctx, c.NewTimer(d))
}

type wallTimer struct {
	t *time.Timer
}

func (w *wallTimer) C() <-chan time.Time { return w.t.C }

func (w *wallTimer) Stop() bool { return w.t.Stop() }

func (w *wallTimer) Reset(d time.Duration) bool {
	// time.Timer.Reset's boolean return is only meaningful when the timer
	// is stopped or drained; our callers always Stop before Reset, so the
	// value is not semantically relied upon.
	active := w.t.Stop()
	if !active {
		// Drain a value that may already be buffered.
		select {
		case <-w.t.C:
		default:
		}
	}
	w.t.Reset(d)
	return active
}
