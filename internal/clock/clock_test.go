package clock_test

import (
	"testing"
	"time"

	"tenantiso/internal/clock"
)

func TestFakeClockAfter(t *testing.T) {
	start := time.Date(2026, 9, 25, 0, 0, 0, 0, time.UTC)
	c := clock.NewFake(start)

	ch := c.After(time.Minute)
	select {
	case <-ch:
		t.Fatal("timer fired before Advance")
	default:
	}
	c.Advance(30 * time.Second)
	select {
	case <-ch:
		t.Fatal("timer fired before deadline")
	default:
	}
	c.Advance(30 * time.Second)
	select {
	case got := <-ch:
		if !got.Equal(start.Add(time.Minute)) {
			t.Fatalf("timer fired with %v, want %v", got, start.Add(time.Minute))
		}
	default:
		t.Fatal("timer did not fire at deadline")
	}
	if !c.Now().Equal(start.Add(time.Minute)) {
		t.Fatalf("Now = %v", c.Now())
	}
}

func TestFakeClockSleep(t *testing.T) {
	c := clock.NewFake(time.Now())
	done := make(chan struct{})
	go func() {
		c.Sleep(time.Second)
		close(done)
	}()
	// Advance in small steps until the goroutine's timer registers and fires.
	deadline := time.Now().Add(2 * time.Second)
	for {
		c.Advance(500 * time.Millisecond)
		select {
		case <-done:
			return
		default:
		}
		if time.Now().After(deadline) {
			t.Fatal("Sleep did not return after Advance")
		}
		time.Sleep(time.Millisecond)
	}
}
