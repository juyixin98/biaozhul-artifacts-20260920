package clock

import (
	"testing"
	"time"
)

func TestManualClockSetAdvance(t *testing.T) {
	t0 := time.Date(2026, 9, 23, 0, 0, 0, 0, time.UTC)
	c := NewManual(t0)
	if c.Now() != t0 {
		t.Fatalf("now=%v want %v", c.Now(), t0)
	}
	if !c.IsManual() {
		t.Fatal("should be manual")
	}
	n, err := c.Advance(30 * time.Second)
	if err != nil || n != t0.Add(30*time.Second) {
		t.Fatalf("advance: %v %v", n, err)
	}
	t1 := t0.Add(2 * time.Hour)
	if err := c.Set(t1); err != nil || c.Now() != t1 {
		t.Fatalf("set: now=%v err=%v", c.Now(), err)
	}
	if _, err := c.Advance(-time.Second); err != ErrNegativeAdvance {
		t.Fatalf("negative advance err=%v", err)
	}
}

func TestRealClockReadOnly(t *testing.T) {
	c := NewReal()
	if c.IsManual() {
		t.Fatal("real clock must not report manual")
	}
	if err := c.Set(time.Now()); err != ErrRealClock {
		t.Fatalf("set real clock err=%v", err)
	}
	if _, err := c.Advance(time.Second); err != ErrRealClock {
		t.Fatalf("advance real clock err=%v", err)
	}
	before := time.Now()
	if c.Now().Before(before.Add(-2 * time.Second)) {
		t.Fatal("real clock should follow wall time")
	}
}
