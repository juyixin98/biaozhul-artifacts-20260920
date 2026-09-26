package clock

import (
	"testing"
	"time"
)

func TestFakeAdvancesAndSleepsWithoutBlocking(t *testing.T) {
	start := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	f := NewFake(start)
	if f.Now() != start {
		t.Fatal("initial time mismatch")
	}
	if f.Since(start) != 0 {
		t.Fatal("since should be zero at start")
	}

	f.Sleep(2 * time.Second) // must not block
	if f.Since(start) != 2*time.Second {
		t.Fatalf("sleep did not advance fake time: %v", f.Since(start))
	}
	got := f.Advance(3 * time.Second)
	if got != start.Add(5*time.Second) {
		t.Fatalf("advance: %v", got)
	}
}

func TestRealClock(t *testing.T) {
	r := Real{}
	before := r.Now()
	r.Sleep(time.Millisecond)
	if r.Since(before) < time.Millisecond {
		t.Fatal("real clock did not advance across sleep")
	}
}

func TestFakeConcurrentAdvance(t *testing.T) {
	f := NewFake(time.Now())
	done := make(chan struct{})
	go func() {
		defer close(done)
		for i := 0; i < 100; i++ {
			f.Advance(time.Nanosecond)
		}
	}()
	for i := 0; i < 100; i++ {
		_ = f.Now()
	}
	<-done
}
