package scheduler

import (
	"testing"
	"time"
)

func TestMockClockTimerFiresOnAdvance(t *testing.T) {
	clk := NewMockClock(time.Unix(1000, 0))
	tm := clk.NewTimer(5 * time.Second)
	select {
	case <-tm.C():
		t.Fatal("timer fired before Advance")
	default:
	}
	clk.Advance(4 * time.Second)
	select {
	case <-tm.C():
		t.Fatal("timer fired too early")
	default:
	}
	clk.Advance(1 * time.Second)
	select {
	case got := <-tm.C():
		if !got.Equal(time.Unix(1005, 0)) {
			t.Fatalf("fire time = %v, want 1005", got)
		}
	default:
		t.Fatal("timer did not fire at deadline")
	}
}

func TestMockClockStopPreventsFire(t *testing.T) {
	clk := NewMockClock(time.Unix(0, 0))
	tm := clk.NewTimer(time.Second)
	if !tm.Stop() {
		t.Fatal("first Stop should report true")
	}
	clk.Advance(2 * time.Second)
	select {
	case <-tm.C():
		t.Fatal("stopped timer fired")
	default:
	}
}

func TestMockClockFiresInDeadlineOrder(t *testing.T) {
	clk := NewMockClock(time.Unix(0, 0))
	later := clk.NewTimer(10 * time.Second)
	earlier := clk.NewTimer(2 * time.Second)
	middle := clk.NewTimer(5 * time.Second)
	clk.Advance(10 * time.Second)
	got := []time.Time{<-earlier.C(), <-middle.C(), <-later.C()}
	if !got[0].Before(got[1]) || !got[1].Before(got[2]) {
		t.Fatalf("fired out of order: %v", got)
	}
}

func TestMockClockZeroAndNegativeDurationsFireImmediately(t *testing.T) {
	clk := NewMockClock(time.Unix(7, 0))
	for _, d := range []time.Duration{0, -time.Second} {
		tm := clk.NewTimer(d)
		select {
		case <-tm.C():
		default:
			t.Fatalf("duration %v should fire immediately", d)
		}
	}
}
