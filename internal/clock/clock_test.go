package clock

import (
	"context"
	"testing"
	"time"
)

// waitSleepers spins (real time) until n virtual timers are registered.
func waitSleepers(t *testing.T, c *Virtual, n int) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		c.mu.Lock()
		got := len(c.sleepers)
		c.mu.Unlock()
		if got >= n {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatalf("timed out waiting for %d registered timers", n)
}

func TestVirtualAdvanceReleasesDueSleepers(t *testing.T) {
	c := NewVirtual()
	start := c.Now()

	fired := make(chan time.Time, 3)
	go func() { fired <- (<-c.After(10 * time.Second)) }()
	go func() { fired <- (<-c.After(5 * time.Second)) }()
	go func() { fired <- (<-c.After(20 * time.Second)) }()
	waitSleepers(t, c, 3)

	c.Advance(10 * time.Second) // 5s and 10s due; 20s not

	got := map[time.Duration]bool{}
	for i := 0; i < 2; i++ {
		select {
		case at := <-fired:
			got[at.Sub(start)] = true
		case <-time.After(time.Second):
			t.Fatal("expected two timers to fire after +10s")
		}
	}
	if !got[5*time.Second] || !got[10*time.Second] {
		t.Fatalf("fired offsets=%v, want 5s and 10s", got)
	}
	if c.Now().Sub(start) != 10*time.Second {
		t.Fatalf("now offset=%s, want 10s", c.Now().Sub(start))
	}
	if !c.HasPendingTimers() {
		t.Fatal("the 20s timer should still be pending")
	}

	c.Advance(10 * time.Second)
	select {
	case at := <-fired:
		if at.Sub(start) != 20*time.Second {
			t.Fatalf("late timer fired at %s, want 20s", at.Sub(start))
		}
	case <-time.After(time.Second):
		t.Fatal("20s timer did not fire")
	}
}

func TestVirtualDoesNotFireBeforeAdvance(t *testing.T) {
	c := NewVirtual()
	ch := c.After(time.Hour)
	c.Advance(time.Second)
	select {
	case <-ch:
		t.Fatal("timer fired before its deadline")
	default:
	}
}

func TestClockContextDeadlineDrivenByVirtualClock(t *testing.T) {
	c := NewVirtual()
	ctx, cancel := WithTimeout(context.Background(), c, 100*time.Millisecond)
	defer cancel()

	if err := ctx.Err(); err != nil {
		t.Fatalf("fresh context err=%v", err)
	}
	c.Advance(99 * time.Millisecond)
	// Let the watcher goroutine run if it was released.
	time.Sleep(10 * time.Millisecond)
	if err := ctx.Err(); err != nil {
		t.Fatalf("context canceled before deadline: %v", err)
	}
	c.Advance(1 * time.Millisecond)
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		if err := ctx.Err(); err != nil {
			if err != context.DeadlineExceeded {
				t.Fatalf("err=%v, want DeadlineExceeded", err)
			}
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatal("clock-driven deadline did not cancel the context")
}

func TestClockContextExplicitCancel(t *testing.T) {
	c := NewVirtual()
	ctx, cancel := WithTimeout(context.Background(), c, time.Hour)
	cancel()
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		if err := ctx.Err(); err == context.Canceled {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatal("explicit cancel did not propagate")
}

func TestClockContextParentCancelPropagates(t *testing.T) {
	c := NewVirtual()
	parent, pcancel := context.WithCancel(context.Background())
	ctx, cancel := WithTimeout(parent, c, time.Hour)
	defer cancel()
	pcancel()
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		if err := ctx.Err(); err == context.Canceled {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatal("parent cancel did not propagate")
}
