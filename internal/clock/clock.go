// Package clock defines the tiny time surface used by the cancellation
// tree. Two implementations exist: Real (wall clock) and Fake (manually
// advanced, so timeout/cleanup races can be tested deterministically).
package clock

import "time"

// Timer is a stoppable timer, mirroring the useful part of time.Timer.
type Timer interface {
	// C returns the channel on which the expiry time is delivered.
	C() <-chan time.Time
	// Stop prevents the timer from firing. It returns true if the call
	// stopped the timer before it expired, false if it had already fired
	// or been stopped.
	Stop() bool
}

// Clock is the subset of time the engine depends on.
type Clock interface {
	// Now returns the current (or simulated) time.
	Now() time.Time
	// Timer creates a timer that fires after d. A non-positive duration
	// fires as soon as possible. Callers should Stop timers they no longer
	// need, exactly as with time.NewTimer.
	Timer(d time.Duration) Timer
}

// Real is the wall-clock implementation.
type Real struct{}

// Now implements Clock.
func (Real) Now() time.Time { return time.Now() }

// Timer implements Clock.
func (Real) Timer(d time.Duration) Timer {
	if d <= 0 {
		t := time.NewTimer(0)
		return &realTimer{t: t}
	}
	return &realTimer{t: time.NewTimer(d)}
}

type realTimer struct{ t *time.Timer }

func (r *realTimer) C() <-chan time.Time { return r.t.C }
func (r *realTimer) Stop() bool          { return r.t.Stop() }
