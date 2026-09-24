package clock

import (
	"testing"
	"time"
)

func TestManualTimerFiresAfterAdvance(t *testing.T) {
	start := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	c := NewManual(start)
	tm := c.NewTimer(time.Second)

	select {
	case <-tm.C():
		t.Fatal("timer fired before any advance")
	default:
	}

	c.Advance(500 * time.Millisecond)
	select {
	case <-tm.C():
		t.Fatal("timer fired before its deadline")
	default:
	}

	c.Advance(500 * time.Millisecond)
	select {
	case at := <-tm.C():
		if !at.Equal(start.Add(time.Second)) {
			t.Fatalf("fired at %v, want %v", at, start.Add(time.Second))
		}
	default:
		t.Fatal("timer did not fire at its deadline")
	}
}

func TestManualTimerStop(t *testing.T) {
	c := NewManual(time.Now())
	tm := c.NewTimer(time.Second)
	if !tm.Stop() {
		t.Fatal("Stop returned false for a pending timer")
	}
	c.Advance(2 * time.Second)
	select {
	case <-tm.C():
		t.Fatal("stopped timer fired")
	default:
	}
}

func TestManualNow(t *testing.T) {
	start := time.Date(2026, 9, 23, 12, 0, 0, 0, time.UTC)
	c := NewManual(start)
	if !c.Now().Equal(start) {
		t.Fatalf("Now = %v, want %v", c.Now(), start)
	}
	c.Advance(3 * time.Hour)
	if !c.Now().Equal(start.Add(3 * time.Hour)) {
		t.Fatalf("Now = %v after advance", c.Now())
	}
}
