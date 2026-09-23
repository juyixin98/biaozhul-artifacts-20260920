// Package clock defines the replaceable time source used by the scheduler.
//
// All scheduling decisions go through Clock.Now and timers created with
// Clock.NewTimer / Clock.ArmTimer, so the same scheduler code can run
// against the wall clock in production or a manually advanced, deterministic
// Fake clock in tests.
package clock

import "time"

// Clock is the time source abstraction.
type Clock interface {
	// Now returns the current time.
	Now() time.Time
	// NewTimer creates a timer that fires after d. A non-positive d fires
	// immediately, mirroring time.NewTimer.
	NewTimer(d time.Duration) Timer
	// ArmTimer atomically replaces old with a timer scheduled to fire at
	// the ABSOLUTE time at. The scheduling decision and timer installation
	// are one operation relative to clock jumps: if the clock is already at
	// or past at when the install happens, the returned timer fires
	// immediately. A zero at schedules an hour from now (never-far-future
	// sentinel), matching a scheduler with nothing due. Passing nil old is
	// equivalent to creating a new timer.
	//
	// Implementations must hold their internal lock across the whole
	// stop-old/arm-new/evaluate-due sequence so a concurrent clock advance
	// can never land in between (that window would otherwise silently drop a
	// deadline under a manually driven clock).
	ArmTimer(old Timer, at time.Time) Timer
}

// Timer is a single-shot timer.
type Timer interface {
	// C returns the channel receiving the firing time.
	C() <-chan time.Time
	// Stop prevents the timer from firing. It returns true if the timer was
	// stopped before it had fired, false if it has already fired or stopped.
	Stop() bool
}

// Wall is a Clock backed by the real system clock.
type Wall struct{}

func (Wall) Now() time.Time { return time.Now() }

func (Wall) NewTimer(d time.Duration) Timer {
	return &wallTimer{t: time.NewTimer(d)}
}

func (Wall) ArmTimer(old Timer, at time.Time) Timer {
	if old != nil {
		if !old.Stop() {
			select {
			case <-old.C():
			default:
			}
		}
	}
	return &wallTimer{t: time.NewTimer(time.Until(at))}
}

type wallTimer struct{ t *time.Timer }

func (w *wallTimer) C() <-chan time.Time { return w.t.C }
func (w *wallTimer) Stop() bool          { return w.t.Stop() }
