// Package clock defines a replaceable time source.
//
// Production code uses RealClock; deterministic tests use FakeClock, whose
// timers and deadline contexts are driven by simulated time instead of wall
// time. The scheduler never calls the time package directly, which makes
// scheduling behaviour fully reproducible in tests.
package clock

import (
	"context"
	"time"
)

// Timer is the minimal timer interface used by the scheduler. Callers always
// stop a timer and create a new one instead of resetting it, so no Reset is
// required.
type Timer interface {
	// C returns the channel on which the (single) firing time is delivered.
	C() <-chan time.Time
	// Stop prevents the timer from firing. It returns false if the timer has
	// already fired or been stopped, best-effort like time.Timer.Stop.
	Stop() bool
}

// Clock abstracts time, timers and deadline contexts.
type Clock interface {
	Now() time.Time
	NewTimer(d time.Duration) Timer
	// After waits for d to elapse on this clock.
	After(d time.Duration) <-chan time.Time
	// DeadlineContext returns a context that is canceled when the clock reaches t.
	DeadlineContext(parent context.Context, t time.Time) (context.Context, context.CancelFunc)
}

// RealClock is the wall-clock implementation.
type RealClock struct{}

func NewRealClock() RealClock { return RealClock{} }

func (RealClock) Now() time.Time { return time.Now() }

type realTimer struct{ t *time.Timer }

func (r realTimer) C() <-chan time.Time { return r.t.C }
func (r realTimer) Stop() bool          { return r.t.Stop() }

func (RealClock) NewTimer(d time.Duration) Timer {
	return realTimer{t: time.NewTimer(d)}
}

func (RealClock) After(d time.Duration) <-chan time.Time {
	return time.After(d)
}

func (c RealClock) DeadlineContext(parent context.Context, t time.Time) (context.Context, context.CancelFunc) {
	return context.WithDeadline(parent, t)
}
