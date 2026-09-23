package clock

import (
	"context"
	"sync"
	"testing"
	"time"
)

func TestVirtualNowMonotonic(t *testing.T) {
	v := NewVirtual(time.Unix(1000, 0))
	if got := v.Now(); got.Unix() != 1000 {
		t.Fatalf("now=%v", got)
	}
	v.Advance(5 * time.Second)
	if v.Now().Unix() != 1005 {
		t.Fatalf("after advance now=%v want 1005", v.Now())
	}
}

func TestVirtualSleepAndAdvance(t *testing.T) {
	v := NewVirtual(time.Unix(0, 0))
	woken := make(chan error, 1)
	go func() {
		woken <- v.Sleep(context.Background(), 250*time.Millisecond)
	}()
	v.WaitForWaiters(1)

	select {
	case <-woken:
		t.Fatal("slept returned before advance")
	default:
	}

	v.Advance(200 * time.Millisecond)
	select {
	case <-woken:
		t.Fatal("slept returned before deadline")
	case <-time.After(20 * time.Millisecond):
	}

	v.Advance(50 * time.Millisecond)
	select {
	case err := <-woken:
		if err != nil {
			t.Fatalf("sleep: %v", err)
		}
	case <-time.After(200 * time.Millisecond):
		t.Fatal("slept not woken after reaching deadline")
	}
}

func TestVirtualSleepCancel(t *testing.T) {
	v := NewVirtual(time.Unix(0, 0))
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- v.Sleep(ctx, time.Hour) }()
	v.WaitForWaiters(1)
	cancel()
	select {
	case err := <-done:
		if err == nil {
			t.Fatal("canceled sleep returned nil")
		}
	case <-time.After(time.Second):
		t.Fatal("cancel did not unblock sleep")
	}
	// Advancing after cancellation must not panic and the timer list should
	// not grow unboundedly.
	v.Advance(time.Hour)
	v.mu.Lock()
	n := len(v.timers)
	v.mu.Unlock()
	if n != 0 {
		t.Fatalf("cancelled timer retained: %d", n)
	}
}

func TestVirtualBatchWake(t *testing.T) {
	v := NewVirtual(time.Unix(0, 0))
	const n = 10
	var wg sync.WaitGroup
	wg.Add(n)
	for i := 0; i < n; i++ {
		go func() {
			defer wg.Done()
			_ = v.Sleep(context.Background(), time.Second)
		}()
	}
	v.WaitForWaiters(n)
	v.Advance(time.Second)
	done := make(chan struct{})
	go func() { wg.Wait(); close(done) }()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("batch waiters not all woken")
	}
}

func TestRealSleepContext(t *testing.T) {
	r := Real{}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := r.Sleep(ctx, time.Hour); err == nil {
		t.Fatal("pre-canceled sleep should error")
	}
	ctx2, cancel2 := context.WithTimeout(context.Background(), 10*time.Millisecond)
	defer cancel2()
	start := time.Now()
	if err := r.Sleep(ctx2, time.Hour); err == nil {
		t.Fatal("deadline sleep should error")
	}
	if time.Since(start) > time.Second {
		t.Fatal("deadline sleep did not return promptly")
	}
}
