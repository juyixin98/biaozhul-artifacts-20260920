package clock

import (
	"testing"
	"time"
)

// Timers fire at exact virtual instants, in insertion order within an instant.
func TestFakeTimerFiresAtInstant(t *testing.T) {
	fc := NewFakeClock(1000)
	var fired []int
	fc.After(fc.Now()+50, func() { fired = append(fired, 1) })
	fc.After(fc.Now()+25, func() { fired = append(fired, 2) })
	fc.After(fc.Now()+50, func() { fired = append(fired, 3) })

	fc.Advance(20)
	if len(fired) != 0 {
		t.Fatalf("early firing: %v", fired)
	}
	fc.Advance(10) // now 1030: timer at 1025 fires
	if len(fired) != 1 || fired[0] != 2 {
		t.Fatalf("at t=1030 fired=%v want [2]", fired)
	}
	fc.Advance(20) // now 1050: both t=1050 timers fire in insertion order
	if len(fired) != 3 || fired[1] != 1 || fired[2] != 3 {
		t.Fatalf("at t=1050 fired=%v want [2 1 3]", fired)
	}
}

// Advancing past several due instants fires all of them.
func TestFakeTimerAdvancePast(t *testing.T) {
	fc := NewFakeClock(0)
	var n int
	fc.After(10, func() { n++ })
	fc.After(20, func() { n++ })
	fc.After(30, func() { n++ })
	fc.Advance(100 * time.Nanosecond)
	if n != 3 {
		t.Fatalf("n=%d want 3", n)
	}
	if int64(fc.Now()) != 100 {
		t.Fatalf("now=%d want 100", fc.Now())
	}
}

// A timer scheduled in the past by a firing job runs on the same Advance.
func TestFakeTimerChainedZeroDelay(t *testing.T) {
	fc := NewFakeClock(0)
	var n int
	var schedule func()
	schedule = func() {
		n++
		if n < 5 {
			fc.After(fc.Now(), schedule)
		}
	}
	fc.After(0, schedule)
	fc.Advance(time.Millisecond)
	if n != 5 {
		t.Fatalf("chain ran %d times want 5", n)
	}
}

// Stopping a pending timer prevents firing.
func TestFakeTimerStop(t *testing.T) {
	fc := NewFakeClock(0)
	var n int
	stop := fc.After(10, func() { n++ })
	fc.After(10, func() { n += 10 })
	if !stop() {
		t.Fatal("stop returned false on pending timer")
	}
	fc.Advance(20 * time.Nanosecond)
	if n != 10 {
		t.Fatalf("n=%d want 10 (stopped timer must not fire)", n)
	}
	if stop() {
		t.Fatal("second stop must return false")
	}
}

// Advance rejects backwards time.
func TestFakeTimerNoBackwards(t *testing.T) {
	fc := NewFakeClock(0)
	defer func() {
		if recover() == nil {
			t.Fatal("expected panic on negative Advance")
		}
	}()
	fc.Advance(-1)
}

// RealClock satisfies both interfaces and never moves meaningfully backwards
// (smoke test: scheduling at an instant in the past runs promptly).
func TestRealClockPastTimer(t *testing.T) {
	rc := NewRealClock()
	done := make(chan struct{})
	stop := rc.After(rc.Now()-1000, func() { close(done) })
	defer stop()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("past-due real timer did not fire within 1s")
	}
}
