package clock_test

import (
	"testing"
	"time"

	"gracefulshutdown/internal/clock"
)

func TestFakeFiresDueTimersInDeadlineOrder(t *testing.T) {
	t.Parallel()
	t0 := time.Date(2026, 9, 25, 12, 0, 0, 0, time.UTC)
	fc := clock.NewFake(t0)

	got := make(chan time.Time, 3)
	_ = fc.After(2 * time.Second) // created first, fires last
	first := fc.After(500 * time.Millisecond)
	second := fc.After(time.Second)

	go func() {
		got <- (<-first)
		got <- (<-second)
	}()

	if got := fc.Now(); !got.Equal(t0) {
		t.Fatalf("now = %v, want %v", got, t0)
	}

	fc.Advance(2 * time.Second)

	for i := 0; i < 2; i++ {
		select {
		case <-got:
		case <-time.After(time.Second):
			t.Fatalf("timer %d did not fire", i)
		}
	}

	if !fc.Now().Equal(t0.Add(2 * time.Second)) {
		t.Fatalf("now = %v, want %v", fc.Now(), t0.Add(2*time.Second))
	}
}

func TestFakeDoesNotFireEarly(t *testing.T) {
	t.Parallel()
	fc := clock.NewFake(time.Unix(0, 0))
	ch := fc.After(time.Minute)
	fc.Advance(time.Second)
	select {
	case <-ch:
		t.Fatal("timer fired before its deadline")
	default:
	}
	fc.Advance(59 * time.Second)
	select {
	case <-ch:
	case <-time.After(time.Second):
		t.Fatal("timer did not fire at deadline")
	}
}
