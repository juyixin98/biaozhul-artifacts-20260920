package scheduler

import (
	"testing"
	"time"
)

func TestFakeClockFiresInDeadlineOrder(t *testing.T) {
	clk := NewFakeClock(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC))
	var order []int
	clk.AfterFunc(30*time.Millisecond, func() { order = append(order, 30) })
	clk.AfterFunc(10*time.Millisecond, func() { order = append(order, 10) })
	clk.AfterFunc(20*time.Millisecond, func() { order = append(order, 20) })
	clk.Advance(30 * time.Millisecond)
	want := []int{10, 20, 30}
	for i := range want {
		if order[i] != want[i] {
			t.Fatalf("fired = %v, want %v", order, want)
		}
	}
	if got := clk.Now(); !got.Equal(time.Date(2026, 1, 1, 0, 0, 0, 30000000, time.UTC)) {
		t.Fatalf("now = %v", got)
	}
}

func TestFakeClockSameDeadlineFiresInRegistrationOrder(t *testing.T) {
	clk := NewFakeClock(time.Time{})
	var order []string
	clk.AfterFunc(10*time.Millisecond, func() { order = append(order, "b") })
	clk.AfterFunc(10*time.Millisecond, func() { order = append(order, "a") })
	// Registration order is b then a; ties must be deterministic (FIFO).
	clk.Advance(10 * time.Millisecond)
	if len(order) != 2 || order[0] != "b" || order[1] != "a" {
		t.Fatalf("order = %v, want [b a]", order)
	}
}

func TestFakeClockTimerDoesNotFireBeforeDeadline(t *testing.T) {
	clk := NewFakeClock(time.Time{})
	tm := clk.NewTimer(100 * time.Millisecond)
	clk.Advance(50 * time.Millisecond)
	select {
	case <-tm.C():
		t.Fatal("timer fired early")
	default:
	}
	clk.Advance(50 * time.Millisecond)
	select {
	case <-tm.C():
	default:
		t.Fatal("timer did not fire at deadline")
	}
}

func TestFakeClockStop(t *testing.T) {
	clk := NewFakeClock(time.Time{})
	fired := false
	tm := clk.AfterFunc(10*time.Millisecond, func() { fired = true })
	if !tm.Stop() {
		t.Fatal("Stop should report true for a pending timer")
	}
	clk.Advance(20 * time.Millisecond)
	if fired {
		t.Fatal("stopped timer fired")
	}
}

// Timers armed during an Advance callback for a deadline inside the same
// window must also fire (matches time.AfterFunc behavior).
func TestFakeClockTimerArmedWithinAdvance(t *testing.T) {
	clk := NewFakeClock(time.Time{})
	got := ""
	clk.AfterFunc(10*time.Millisecond, func() {
		got += "a"
		clk.AfterFunc(5*time.Millisecond, func() { got += "b" })
	})
	clk.Advance(20 * time.Millisecond)
	if got != "ab" {
		t.Fatalf("got = %q, want ab", got)
	}
}
