package clock

import (
	"context"
	"testing"
	"time"
)

func TestRealClockSleep(t *testing.T) {
	c := RealClock{}
	start := c.Now()
	if err := c.Sleep(context.Background(), 5*time.Millisecond); err != nil {
		t.Fatalf("sleep: %v", err)
	}
	if c.Now().Sub(start) < 4*time.Millisecond {
		t.Error("real sleep returned too early")
	}
}

func TestRealClockSleepCanceled(t *testing.T) {
	c := RealClock{}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := c.Sleep(ctx, time.Hour); err == nil {
		t.Error("want context error on canceled sleep")
	}
}

func TestFakeClockDoesNotAdvanceByItself(t *testing.T) {
	start := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	c := NewFake(start)
	if !c.Now().Equal(start) {
		t.Fatalf("now = %v", c.Now())
	}
	time.Sleep(2 * time.Millisecond)
	if !c.Now().Equal(start) {
		t.Fatalf("fake clock advanced on its own to %v", c.Now())
	}
}

func TestFakeClockReleasesWaiter(t *testing.T) {
	c := NewFake(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC))
	done := make(chan error, 1)
	go func() { done <- c.Sleep(context.Background(), 10*time.Second) }()

	waitForWaiters(c, 1)
	c.Advance(9 * time.Second)
	if c.WaiterCount() != 1 {
		t.Fatalf("waiter released before deadline: count=%d", c.WaiterCount())
	}
	c.Advance(1 * time.Second)
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("sleep: %v", err)
		}
	case <-time.After(time.Second):
		t.Fatal("waiter not released at deadline")
	}
}

func TestFakeClockSleepCanceled(t *testing.T) {
	c := NewFake(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC))
	ctx, cancel := context.WithCancel(context.Background())
	errCh := make(chan error, 1)
	go func() { errCh <- c.Sleep(ctx, time.Hour) }()
	waitForWaiters(c, 1)
	cancel()
	select {
	case err := <-errCh:
		if err == nil {
			t.Error("want context error")
		}
	case <-time.After(time.Second):
		t.Fatal("cancel did not unblock sleep")
	}
}

func TestFakeClockNonPositiveAndAdvance(t *testing.T) {
	c := NewFake(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC))
	if err := c.Sleep(context.Background(), 0); err != nil {
		t.Errorf("zero sleep: %v", err)
	}
	before := c.Now()
	c.Advance(0)
	if !c.Now().Equal(before) {
		t.Error("zero advance moved the clock")
	}
}

func waitForWaiters(c *FakeClock, want int) {
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		if c.WaiterCount() == want {
			return
		}
		time.Sleep(time.Millisecond)
	}
}
