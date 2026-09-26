// Package clock provides a controllable time abstraction.
//
// RealClock delegates to wall-clock time; FakeClock only advances when a
// test calls Advance, which makes timeout/cancellation logic deterministic
// and free of real sleeps.
package clock

import (
	"context"
	"time"
)

// Clock is the minimal time surface used by the project.
type Clock interface {
	// Now returns the current (or simulated) time.
	Now() time.Time
	// NewTimer creates a timer that fires after d. A non-positive d fires
	// immediately, mirroring time.NewTimer.
	NewTimer(d time.Duration) Timer
	// After is shorthand for NewTimer(d).C().
	After(d time.Duration) <-chan time.Time
	// Sleep blocks for d or until ctx is canceled. It returns nil when the
	// duration elapsed, otherwise ctx.Err().
	Sleep(ctx context.Context, d time.Duration) error
}

// Timer is the channel-based timer returned by Clock.NewTimer.
type Timer interface {
	// C returns the channel on which the firing time is delivered.
	C() <-chan time.Time
	// Stop prevents the timer from firing. It reports whether the call
	// stopped a pending timer (false when it already fired or was stopped).
	Stop() bool
	// Reset reschedules (or re-arms) the timer for d. It reports whether
	// the timer had been pending when reset.
	Reset(d time.Duration) bool
}

// sleepOn waits on a timer and cancels it on context abort.
func sleepOn(ctx context.Context, t Timer) error {
	defer t.Stop()
	select {
	case <-t.C():
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}
