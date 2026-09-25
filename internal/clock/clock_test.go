package clock

import "testing"

func TestVirtualClockAdvance(t *testing.T) {
	c := NewVirtualClock(0)
	if c.Now() != 0 {
		t.Fatalf("initial Now = %d, want 0", c.Now())
	}
	c.Advance(3)
	c.Advance(2)
	if c.Now() != 5 {
		t.Errorf("Now = %d, want 5", c.Now())
	}
}

func TestVirtualClockNegativePanics(t *testing.T) {
	defer func() {
		if recover() == nil {
			t.Fatal("expected panic on negative advance")
		}
	}()
	NewVirtualClock(0).Advance(-1)
}

func TestVirtualClockDeterministicWallTime(t *testing.T) {
	c := NewVirtualClock(1000)
	if got := c.WallTime().UnixMilli(); got != 1000 {
		t.Errorf("WallTime unixmilli = %d, want 1000", got)
	}
}
