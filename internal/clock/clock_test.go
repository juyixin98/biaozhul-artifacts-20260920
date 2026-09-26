package clock

import (
	"testing"
	"time"
)

func TestFakeAdvanceIsImmutable(t *testing.T) {
	t0 := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	f := NewFake(t0)
	next := f.Advance(5 * time.Minute)
	if !f.Now().Equal(t0) {
		t.Fatal("Advance mutated the receiver")
	}
	if !next.Now().Equal(t0.Add(5 * time.Minute)) {
		t.Fatalf("advanced clock = %v", next.Now())
	}
}

func TestFakeNormalizesToUTC(t *testing.T) {
	loc := time.FixedZone("+02:00", 2*3600)
	f := NewFake(time.Date(2026, 1, 1, 2, 0, 0, 0, loc))
	if f.Now().Location() != time.UTC || f.Now().Hour() != 0 {
		t.Fatalf("not normalized to UTC: %v", f.Now())
	}
}

func TestSystemNow(t *testing.T) {
	before := time.Now().UTC()
	got := System{}.Now()
	after := time.Now().UTC()
	if got.Before(before) || got.After(after) {
		t.Fatalf("System.Now() %v outside window", got)
	}
}
