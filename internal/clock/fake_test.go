package clock

import (
	"context"
	"testing"
	"time"
)

func TestFakeClock_TimerFiresOnAdvance(t *testing.T) {
	fc := NewFakeClock(time.UnixMilli(0))
	got := make(chan time.Time, 1)
	tm := fc.NewTimer(50 * time.Millisecond)
	go func() { got <- <-tm.C() }()

	if n := fc.PendingTimers(); n != 1 {
		t.Fatalf("pending timers = %d, want 1", n)
	}
	fc.Advance(49 * time.Millisecond)
	select {
	case <-got:
		t.Fatal("timer fired 1ms early")
	default:
	}
	fc.Advance(1 * time.Millisecond)
	select {
	case <-got:
	case <-time.After(time.Second):
		t.Fatal("timer did not fire after deadline")
	}
	if n := fc.PendingTimers(); n != 0 {
		t.Fatalf("pending timers after fire = %d, want 0", n)
	}
}

func TestFakeClock_ImmediateTimer(t *testing.T) {
	fc := NewFakeClock(time.UnixMilli(0))
	tm := fc.NewTimer(0)
	select {
	case <-tm.C():
	case <-time.After(time.Second):
		t.Fatal("zero-duration timer did not fire immediately")
	}
}

func TestFakeClock_SleepCanceled(t *testing.T) {
	fc := NewFakeClock(time.UnixMilli(0))
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- fc.Sleep(ctx, time.Second) }()

	// Wait until the timer is registered, then cancel without advancing.
	waitFor(t, func() bool { return fc.PendingTimers() == 1 })
	cancel()
	select {
	case err := <-done:
		if err != context.Canceled {
			t.Fatalf("sleep err = %v, want context.Canceled", err)
		}
	case <-time.After(time.Second):
		t.Fatal("sleep did not return after cancel")
	}
	waitFor(t, func() bool { return fc.PendingTimers() == 0 })
}

func TestFakeClock_FiresInDeadlineOrder(t *testing.T) {
	fc := NewFakeClock(time.UnixMilli(0))
	t10 := fc.NewTimer(10 * time.Millisecond)
	t20 := fc.NewTimer(20 * time.Millisecond)
	t30 := fc.NewTimer(30 * time.Millisecond)
	waitFor(t, func() bool { return fc.PendingTimers() == 3 })

	ready := func(tm Timer) bool {
		select {
		case <-tm.C():
			return true
		default:
			return false
		}
	}

	fc.Advance(10 * time.Millisecond)
	if !ready(t10) {
		t.Fatal("10ms timer did not fire exactly at its deadline")
	}
	if ready(t20) || ready(t30) {
		t.Fatal("a later timer fired early at 10ms")
	}
	fc.Advance(10 * time.Millisecond)
	if !ready(t20) {
		t.Fatal("20ms timer did not fire at its deadline")
	}
	if ready(t30) {
		t.Fatal("30ms timer fired early at 20ms")
	}
	fc.Advance(10 * time.Millisecond)
	if !ready(t30) {
		t.Fatal("30ms timer did not fire at its deadline")
	}
	if n := fc.PendingTimers(); n != 0 {
		t.Fatalf("pending = %d, want 0 after all deadlines", n)
	}
}

func TestRealClock_NowAdvances(t *testing.T) {
	rc := NewRealClock()
	a := rc.Now()
	time.Sleep(2 * time.Millisecond)
	if !rc.Now().After(a) {
		t.Fatal("real clock Now did not advance")
	}
}

func TestFakeClock_AdvanceTo(t *testing.T) {
	fc := NewFakeClock(time.UnixMilli(1000))
	tm := fc.NewTimer(5 * time.Millisecond)
	fc.AdvanceTo(time.UnixMilli(1005))
	select {
	case <-tm.C():
	case <-time.After(time.Second):
		t.Fatal("timer did not fire after AdvanceTo")
	}
}

func TestFakeClock_StopTimer(t *testing.T) {
	fc := NewFakeClock(time.UnixMilli(0))
	tm := fc.NewTimer(time.Second)
	if !tm.Stop() {
		t.Fatal("Stop on a pending timer must report true")
	}
	fc.Advance(time.Hour)
	select {
	case <-tm.C():
		t.Fatal("stopped timer fired")
	default:
	}
	if tm.Stop() {
		t.Fatal("double Stop must report false")
	}
}

func TestFakeClock_NegativeAdvancePanics(t *testing.T) {
	fc := NewFakeClock(time.UnixMilli(0))
	defer func() {
		if rec := recover(); rec == nil {
			t.Fatal("expected panic on negative Advance")
		}
	}()
	fc.Advance(-time.Second)
}

func TestRealClock_TimerAndSleep(t *testing.T) {
	rc := NewRealClock()
	tm := rc.NewTimer(5 * time.Millisecond)
	select {
	case <-tm.C():
	case <-time.After(time.Second):
		t.Fatal("real timer did not fire")
	}
	select {
	case <-rc.After(5 * time.Millisecond):
	case <-time.After(time.Second):
		t.Fatal("real After did not fire")
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := rc.Sleep(ctx, time.Second); err != context.Canceled {
		t.Fatalf("real sleep err = %v, want canceled", err)
	}
}

func TestFakeClock_TimerReset(t *testing.T) {
	fc := NewFakeClock(time.UnixMilli(0))
	tm := fc.NewTimer(100 * time.Millisecond)
	if !tm.Reset(10 * time.Millisecond) {
		// Reset on a pending timer reports true (it was active).
		t.Fatal("Reset on pending timer must report true")
	}
	waitFor(t, func() bool { return fc.PendingTimers() == 1 })
	fc.Advance(10 * time.Millisecond)
	select {
	case <-tm.C():
	case <-time.After(time.Second):
		t.Fatal("timer did not fire on rescheduled deadline")
	}
}

func TestFakeClock_TimerResetAfterFire(t *testing.T) {
	fc := NewFakeClock(time.UnixMilli(0))
	tm := fc.NewTimer(time.Millisecond)
	fc.Advance(time.Millisecond)
	select {
	case <-tm.C():
	case <-time.After(time.Second):
		t.Fatal("timer did not fire")
	}
	tm.Reset(time.Hour)
	waitFor(t, func() bool { return fc.PendingTimers() == 1 })
	fc.Advance(time.Hour)
	select {
	case <-tm.C():
	case <-time.After(time.Second):
		t.Fatal("reset timer did not fire after new deadline")
	}
}

func TestFakeClock_AfterChannel(t *testing.T) {
	fc := NewFakeClock(time.UnixMilli(0))
	if got := fc.Now().UnixMilli(); got != 0 {
		t.Fatalf("Now = %d, want 0", got)
	}
	ch := fc.After(5 * time.Millisecond)
	fc.Advance(5 * time.Millisecond)
	select {
	case <-ch:
	case <-time.After(time.Second):
		t.Fatal("After channel did not deliver")
	}
}

func TestRealClock_TimerReset(t *testing.T) {
	rc := NewRealClock()
	tm := rc.NewTimer(time.Hour)
	tm.Stop()
	tm.Reset(time.Millisecond)
	select {
	case <-tm.C():
	case <-time.After(time.Second):
		t.Fatal("reset real timer did not fire")
	}
}

func waitFor(t *testing.T, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatal("condition not met within timeout")
}
