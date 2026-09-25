package clock

import (
	"testing"
	"time"
)

func TestManualAfterFiresOnAdvance(t *testing.T) {
	c := NewManual(time.Unix(0, 0))
	ch := c.After(5 * time.Second)

	select {
	case <-ch:
		t.Fatal("timer fired before Advance")
	default:
	}

	c.Advance(4 * time.Second)
	select {
	case <-ch:
		t.Fatal("timer fired before its deadline")
	default:
	}

	c.Advance(1 * time.Second)
	select {
	case got := <-ch:
		if !got.Equal(time.Unix(5, 0)) {
			t.Fatalf("timer delivered %v, want %v", got, time.Unix(5, 0))
		}
	default:
		t.Fatal("timer did not fire at its deadline")
	}
	if c.Pending() != 0 {
		t.Fatalf("Pending() = %d, want 0", c.Pending())
	}
}

func TestManualNowAdvances(t *testing.T) {
	start := time.Unix(100, 0)
	c := NewManual(start)
	if !c.Now().Equal(start) {
		t.Fatalf("Now() = %v, want %v", c.Now(), start)
	}
	c.Advance(2 * time.Minute)
	if !c.Now().Equal(start.Add(2 * time.Minute)) {
		t.Fatalf("Now() = %v after Advance", c.Now())
	}
}

func TestManualMultipleTimers(t *testing.T) {
	c := NewManual(time.Unix(0, 0))
	a := c.After(1 * time.Second)
	b := c.After(10 * time.Second)

	c.Advance(5 * time.Second)
	select {
	case <-a:
	default:
		t.Fatal("timer a should have fired")
	}
	select {
	case <-b:
		t.Fatal("timer b fired too early")
	default:
	}
	if c.Pending() != 1 {
		t.Fatalf("Pending() = %d, want 1", c.Pending())
	}
}
