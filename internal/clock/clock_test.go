package clock

import (
	"context"
	"testing"
	"time"
)

// Fake fires every due timer when advanced past its deadline. With one
// advance crossing all three deadlines all three sleepers are released.
// (Channel close order across independently-scheduled goroutines is not a
// synchronization guarantee, so receive order is not asserted.)
func TestFakeFiresTimersInOrder(t *testing.T) {
	fk := NewFake(time.Unix(1_700_000_000, 0))
	fired := make(chan time.Duration, 3)

	ds := []time.Duration{40 * time.Millisecond, 10 * time.Millisecond, 25 * time.Millisecond}
	for _, d := range ds {
		go func(d time.Duration) {
			fk.Sleep(context.Background(), d)
			fired <- d
		}(d)
	}

	for fk.Pending() != 3 {
		time.Sleep(time.Millisecond)
	}
	fk.Advance(50 * time.Millisecond)

	got := map[time.Duration]bool{}
	for i := 0; i < 3; i++ {
		select {
		case d := <-fired:
			got[d] = true
		case <-time.After(time.Second):
			t.Fatalf("timer %d did not fire", i)
		}
	}
	for _, d := range ds {
		if !got[d] {
			t.Fatalf("timer %s never fired", d)
		}
	}
	if fk.Pending() != 0 {
		t.Fatalf("pending=%d, want 0", fk.Pending())
	}
}

func TestFakePartialAdvanceFiresOnlyDue(t *testing.T) {
	fk := NewFake(time.Unix(1_700_000_000, 0))
	fired := make(chan time.Duration, 3)
	d := 10 * time.Millisecond
	go func() {
		fk.Sleep(context.Background(), d)
		fired <- d
	}()
	for fk.Pending() != 1 {
		time.Sleep(time.Millisecond)
	}
	fk.Advance(10 * time.Millisecond)
	select {
	case <-fired:
	case <-time.After(time.Second):
		t.Fatal("due timer did not fire")
	}
}

func TestFakeSleepCanceledBeforeDeadline(t *testing.T) {
	fk := NewFake(time.Unix(1_700_000_000, 0))
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan bool, 1)
	go func() { done <- fk.Sleep(ctx, time.Second) }()

	for fk.Pending() == 0 {
		time.Sleep(time.Millisecond)
	}
	cancel()
	if fired := <-done; fired {
		t.Fatal("Sleep reported fired after cancellation")
	}
	if fk.Pending() != 0 {
		t.Fatalf("canceled timer left pending: %d", fk.Pending())
	}
}

func TestRealSleepContextCancel(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if (Real{}).Sleep(ctx, time.Minute) {
		t.Fatal("Real.Sleep returned true on canceled context")
	}
}
