package clock

import (
	"testing"
	"time"
)

func TestFakeClock(t *testing.T) {
	start := time.Date(2026, 9, 25, 0, 0, 0, 0, time.UTC)
	c := NewFake(start)
	if !c.Now().Equal(start) {
		t.Fatalf("Now = %v, want %v", c.Now(), start)
	}
	c.Sleep(time.Second)
	c.Advance(time.Minute)
	if !c.Now().Equal(start.Add(61 * time.Second)) {
		t.Fatalf("Now = %v after advance", c.Now())
	}
}

func TestRealClock(t *testing.T) {
	var c Real
	before := time.Now()
	c.Sleep(time.Millisecond)
	if c.Now().Before(before) {
		t.Fatal("real clock went backwards")
	}
}
