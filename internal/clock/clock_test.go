package clock

import (
	"testing"
	"time"
)

func TestFakeClockSetAndAdvance(t *testing.T) {
	base := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	f := NewFake(base)
	if !f.Now().Equal(base) {
		t.Fatalf("initial: %v", f.Now())
	}
	f.Advance(90 * time.Second)
	if got := f.Now(); !got.Equal(base.Add(90 * time.Second)) {
		t.Fatalf("after advance: %v", got)
	}
	target := time.Date(2030, 6, 15, 12, 0, 0, 0, time.UTC)
	f.Set(target)
	if !f.Now().Equal(target) {
		t.Fatalf("after set: %v", f.Now())
	}
}

func TestRealClock(t *testing.T) {
	before := time.Now().UTC()
	got := Real{}.Now()
	after := time.Now().UTC()
	if got.Before(before) || got.After(after) {
		t.Fatalf("real clock out of range: %v not in [%v, %v]", got, before, after)
	}
}
