package clock_test

import (
	"testing"
	"time"

	"example.com/tenantiso/internal/clock"
)

func TestFakeClockAdvance(t *testing.T) {
	start := time.Date(2026, 9, 25, 12, 0, 0, 0, time.UTC)
	c := clock.NewFake(start)
	if !c.Now().Equal(start) {
		t.Fatalf("initial: %v", c.Now())
	}
	c.Advance(90 * time.Minute)
	if got := c.Now(); !got.Equal(start.Add(90 * time.Minute)) {
		t.Fatalf("advanced: %v", got)
	}
}

func TestRealClock(t *testing.T) {
	if (clock.Real{}).Now().IsZero() {
		t.Fatal("real clock returned zero time")
	}
}
