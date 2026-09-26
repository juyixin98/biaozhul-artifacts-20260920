package clock

import (
	"testing"
	"time"
)

func TestFakeDeterministic(t *testing.T) {
	start := time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC)
	f := NewFake(start)
	if !f.Now().Equal(start) {
		t.Fatalf("now = %v, want %v", f.Now(), start)
	}
	next := f.Add(10 * time.Second)
	want := start.Add(10 * time.Second)
	if !next.Equal(want) || !f.Now().Equal(want) {
		t.Fatalf("after add: %v / %v, want %v", next, f.Now(), want)
	}
}

func TestRealAdvances(t *testing.T) {
	r := Real{}
	t1 := r.Now()
	time.Sleep(2 * time.Millisecond)
	if !r.Now().After(t1) {
		t.Fatalf("real clock did not advance: %v -> %v", t1, r.Now())
	}
}
