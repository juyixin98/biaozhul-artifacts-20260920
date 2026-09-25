package batchagg

import (
	"testing"
	"time"
)

func TestVirtualClock_TimerFiresOnlyAfterAdvance(t *testing.T) {
	vc := NewVirtualClock()
	tm := vc.NewTimer(100 * time.Millisecond)

	select {
	case <-tm.C():
		t.Fatal("timer fired before Advance")
	default:
	}

	vc.Advance(50 * time.Millisecond)
	select {
	case <-tm.C():
		t.Fatal("timer fired at 50ms for a 100ms timer")
	default:
	}

	at := vc.Advance(50 * time.Millisecond)
	if !at.Equal(time.Unix(0, 100_000_000).UTC()) {
		t.Fatalf("unexpected virtual time %v", at)
	}
	select {
	case got := <-tm.C():
		if !got.Equal(time.Unix(0, 100_000_000).UTC()) {
			t.Fatalf("timer delivered %v", got)
		}
	default:
		t.Fatal("timer did not fire after reaching deadline")
	}
}

func TestVirtualClock_StopPreventsFire(t *testing.T) {
	vc := NewVirtualClock()
	tm := vc.NewTimer(time.Second)
	if !tm.Stop() {
		t.Fatal("first Stop should return true for active timer")
	}
	vc.Advance(2 * time.Second)
	select {
	case <-tm.C():
		t.Fatal("stopped timer fired")
	default:
	}
	if tm.Stop() {
		t.Fatal("second Stop should return false")
	}
}

func TestVirtualClock_ResetReschedules(t *testing.T) {
	vc := NewVirtualClock()
	tm := vc.NewTimer(time.Hour)
	tm.Reset(10 * time.Millisecond)
	vc.Advance(9 * time.Millisecond)
	select {
	case <-tm.C():
		t.Fatal("timer fired before reset deadline")
	default:
	}
	vc.Advance(1 * time.Millisecond)
	select {
	case <-tm.C():
	default:
		t.Fatal("timer did not fire at reset deadline")
	}
}

func TestVirtualClock_TimersFireInDeadlineOrder(t *testing.T) {
	vc := NewVirtualClock()
	t1 := vc.NewTimer(30 * time.Millisecond)
	t2 := vc.NewTimer(10 * time.Millisecond)
	t3 := vc.NewTimer(20 * time.Millisecond)

	vc.Advance(30 * time.Millisecond)

	// All three channels hold their expiry (buffered size 1); reading in any
	// order works, but the sent times must be ordered 10ms, 20ms, 30ms.
	fired := []time.Time{<-t2.C(), <-t3.C(), <-t1.C()}
	want := []time.Duration{10 * time.Millisecond, 20 * time.Millisecond, 30 * time.Millisecond}
	for i, w := range want {
		if !fired[i].Equal(time.Unix(0, int64(w)).UTC()) {
			t.Fatalf("fire %d: got %v want %v", i, fired[i], w)
		}
	}
}
