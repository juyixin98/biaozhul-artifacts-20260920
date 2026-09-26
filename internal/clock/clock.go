// Package clock provides a controllable clock abstraction so that
// shutdown timing (drain timeouts, backoff, fake-service latency) can be
// driven deterministically in tests and by the fault-injection client.
package clock

import "time"

// Timer abstracts time.Timer.
type Timer interface {
	C() <-chan time.Time
	Stop() bool
}

// Clock is the time source used across the whole process. Production code
// uses Real; tests and fault injection may substitute Fake.
type Clock interface {
	Now() time.Time
	After(d time.Duration) <-chan time.Time
	Sleep(d time.Duration)
	NewTimer(d time.Duration) Timer
}

// Real is the wall-clock implementation.
type Real struct{}

func (Real) Now() time.Time                         { return time.Now() }
func (Real) After(d time.Duration) <-chan time.Time { return time.After(d) }
func (Real) Sleep(d time.Duration)                  { time.Sleep(d) }

type realTimer struct{ t *time.Timer }

func (r realTimer) C() <-chan time.Time { return r.t.C }
func (r realTimer) Stop() bool          { return r.t.Stop() }

func (Real) NewTimer(d time.Duration) Timer { return realTimer{t: time.NewTimer(d)} }
