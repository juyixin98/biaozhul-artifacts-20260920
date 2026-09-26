package clock

import (
	"context"
	"testing"
	"time"
)

func TestFakeSleepWakesOnAdvance(t *testing.T) {
	c := NewFake(time.Unix(0, 0))
	done := make(chan error, 1)
	go func() { done <- c.Sleep(context.Background(), 5*time.Second) }()
	waitForWaiters(t, c, 1)

	c.Advance(4 * time.Second)
	select {
	case <-done:
		t.Fatal("sleeper woke before its wake time")
	case <-time.After(20 * time.Millisecond):
	}
	c.Advance(1 * time.Second)
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("sleep: %v", err)
		}
	case <-time.After(time.Second):
		t.Fatal("sleeper did not wake after advance past wake time")
	}
}

func waitForWaiters(t *testing.T, c *Fake, n int) {
	t.Helper()
	for i := 0; i < 1000; i++ {
		if c.Waiters() >= n {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatalf("timed out waiting for %d sleepers", n)
}

func TestFakeSleepRespectsCancellation(t *testing.T) {
	c := NewFake(time.Unix(0, 0))
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- c.Sleep(ctx, time.Hour) }()
	cancel()
	select {
	case err := <-done:
		if err != context.Canceled {
			t.Fatalf("want context.Canceled, got %v", err)
		}
	case <-time.After(time.Second):
		t.Fatal("sleep did not return after cancel")
	}
}

func TestRealSleepRespectsCancellation(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := (Real{}).Sleep(ctx, time.Hour); err != context.Canceled {
		t.Fatalf("want context.Canceled, got %v", err)
	}
}
