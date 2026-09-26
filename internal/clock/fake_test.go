package clock

import (
	"runtime"
	"testing"
	"time"
)

func TestFakeTimerFiresOnAdvance(t *testing.T) {
	clk := NewFake(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC))
	t1 := clk.Timer(10 * time.Second)
	t2 := clk.Timer(20 * time.Second)

	if got := clk.PendingTimers(); got != 2 {
		t.Fatalf("PendingTimers = %d, want 2", got)
	}
	select {
	case <-t1.C():
		t.Fatal("timer fired before clock advanced")
	default:
	}

	clk.Advance(10 * time.Second)
	select {
	case <-t1.C():
	default:
		t.Fatal("10s timer did not fire")
	}
	select {
	case <-t2.C():
		t.Fatal("20s timer fired too early")
	default:
	}
	if got := clk.PendingTimers(); got != 1 {
		t.Fatalf("PendingTimers = %d, want 1", got)
	}

	clk.Advance(10 * time.Second)
	select {
	case <-t2.C():
	default:
		t.Fatal("20s timer did not fire after second advance")
	}
	if got := clk.PendingTimers(); got != 0 {
		t.Fatalf("PendingTimers = %d, want 0 after all fired", got)
	}
}

func TestFakeTimerFiresInWakeOrder(t *testing.T) {
	clk := NewFake(time.Time{})
	late := clk.Timer(30 * time.Millisecond)
	early := clk.Timer(5 * time.Millisecond)
	middle := clk.Timer(10 * time.Millisecond)

	clk.Advance(30 * time.Millisecond)
	for _, want := range []<-chan time.Time{early.C(), middle.C(), late.C()} {
		select {
		case <-want:
		default:
			t.Fatal("expected timer to have fired")
		}
	}
}

func TestFakeTimerStopDetaches(t *testing.T) {
	clk := NewFake(time.Time{})
	tm := clk.Timer(time.Minute)
	if !tm.Stop() {
		t.Fatal("Stop returned false for a pending timer")
	}
	if got := clk.PendingTimers(); got != 0 {
		t.Fatalf("PendingTimers = %d, want 0 after stop", got)
	}
	clk.Advance(2 * time.Minute)
	select {
	case <-tm.C():
		t.Fatal("stopped timer fired")
	default:
	}
	if tm.Stop() {
		t.Fatal("second Stop should report false")
	}
}

func TestFakeZeroDurationReady(t *testing.T) {
	clk := NewFake(time.Time{})
	tm := clk.Timer(0)
	select {
	case <-tm.C():
	default:
		t.Fatal("zero-duration timer should be ready immediately")
	}
	clk.Advance(time.Second)
	if got := clk.PendingTimers(); got != 0 {
		t.Fatalf("PendingTimers = %d, want 0", got)
	}
}

func TestFakeChainedTimersWithinWindow(t *testing.T) {
	clk := NewFake(time.Time{})
	first := clk.Timer(10 * time.Millisecond)
	go func() {
		<-first.C()
		second := clk.Timer(5 * time.Millisecond)
		<-second.C()
	}()
	clk.Advance(10 * time.Millisecond)
	// Wait until the woken goroutine has registered the chained timer.
	deadline := time.Now().Add(2 * time.Second)
	for clk.PendingTimers() != 1 && time.Now().Before(deadline) {
		runtime.Gosched()
	}
	if got := clk.PendingTimers(); got != 1 {
		t.Fatalf("PendingTimers = %d, want 1 chained timer registered", got)
	}
	clk.Advance(10 * time.Millisecond)
	deadline = time.Now().Add(2 * time.Second)
	for clk.PendingTimers() != 0 && time.Now().Before(deadline) {
		runtime.Gosched()
	}
	if got := clk.PendingTimers(); got != 0 {
		t.Fatalf("PendingTimers = %d, want 0 after chained timer fired", got)
	}
}
