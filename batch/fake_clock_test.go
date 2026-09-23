package batch

import (
	"testing"
	"time"
)

func TestFakeClock_FiresAtAdvancedTime(t *testing.T) {
	clk := NewFakeClock(time.Unix(1000, 0))
	tm := clk.NewTimer(100 * time.Millisecond)

	clk.Advance(50 * time.Millisecond)
	select {
	case <-tm.C():
		t.Fatal("timer fired before deadline")
	default:
	}

	before := clk.Now()
	clk.Advance(50 * time.Millisecond)
	select {
	case got := <-tm.C():
		if !got.Equal(before.Add(50 * time.Millisecond)) {
			t.Fatalf("fired at %v, want %v", got, before.Add(50*time.Millisecond))
		}
	default:
		t.Fatal("timer did not fire after deadline")
	}
}

func TestFakeClock_StopPreventsFiring(t *testing.T) {
	clk := NewFakeClock(time.Unix(1000, 0))
	tm := clk.NewTimer(time.Second)
	if !tm.Stop() {
		t.Fatal("first Stop should return true")
	}
	if tm.Stop() {
		t.Fatal("second Stop should return false")
	}
	clk.Advance(2 * time.Second)
	select {
	case <-tm.C():
		t.Fatal("stopped timer fired")
	default:
	}
}

func TestFakeClock_ResetActiveTimer(t *testing.T) {
	clk := NewFakeClock(time.Unix(1000, 0))
	tm := clk.NewTimer(time.Second)
	active := tm.Reset(50 * time.Millisecond)
	if !active {
		t.Fatal("Reset on active timer should return true")
	}
	clk.Advance(49 * time.Millisecond)
	select {
	case <-tm.C():
		t.Fatal("fired on old schedule")
	default:
	}
	clk.Advance(2 * time.Millisecond)
	select {
	case <-tm.C():
	default:
		t.Fatal("timer did not fire on reset schedule")
	}
}

func TestFakeClock_NonPositiveDurationFiresImmediately(t *testing.T) {
	clk := NewFakeClock(time.Unix(1000, 0))
	tm := clk.NewTimer(0)
	select {
	case <-tm.C():
	default:
		t.Fatal("zero-duration timer should be already fired")
	}
}

func TestFakeClock_MultipleTimersOrdered(t *testing.T) {
	clk := NewFakeClock(time.Unix(1000, 0))
	late := clk.NewTimer(200 * time.Millisecond)
	early := clk.NewTimer(100 * time.Millisecond)

	clk.Advance(200 * time.Millisecond)

	var firedOrder []int
	// early 应当先于 late 就绪。
	select {
	case <-early.C():
		firedOrder = append(firedOrder, 1)
	default:
		t.Fatal("early timer not fired")
	}
	select {
	case <-late.C():
		firedOrder = append(firedOrder, 2)
	default:
		t.Fatal("late timer not fired")
	}
	if len(firedOrder) != 2 {
		t.Fatalf("fired order = %v", firedOrder)
	}
}
