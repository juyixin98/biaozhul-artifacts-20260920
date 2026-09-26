package clock

import (
	"context"
	"testing"
	"time"
)

func TestFakeSleepBlocksUntilAdvance(t *testing.T) {
	f := NewFake(time.Unix(0, 0))
	done := make(chan error, 1)
	go func() { done <- f.Sleep(context.Background(), 5*time.Second) }()
	for f.Waiters() == 0 { // wait until the sleeper is registered
		time.Sleep(time.Millisecond)
	}

	f.Advance(4 * time.Second)
	select {
	case <-done:
		t.Fatal("sleep returned before deadline")
	case <-time.After(50 * time.Millisecond):
	}
	f.Advance(1 * time.Second)
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("sleep: %v", err)
		}
	case <-time.After(time.Second):
		t.Fatal("sleep did not wake after advance past deadline")
	}
}

func TestFakeSleepHonorsContext(t *testing.T) {
	f := NewFake(time.Unix(0, 0))
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- f.Sleep(ctx, time.Hour) }()
	cancel()
	select {
	case err := <-done:
		if err == nil {
			t.Fatal("expected context error")
		}
	case <-time.After(time.Second):
		t.Fatal("sleep did not unblock on cancel")
	}
}

func TestRealSleep(t *testing.T) {
	start := time.Now()
	if err := (Real{}).Sleep(context.Background(), 5*time.Millisecond); err != nil {
		t.Fatalf("sleep: %v", err)
	}
	if time.Since(start) < 5*time.Millisecond {
		t.Fatal("real sleep returned early")
	}
}
