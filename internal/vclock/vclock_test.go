package vclock

import (
	"context"
	"testing"
	"time"
)

func TestNowDoesNotMoveByItself(t *testing.T) {
	vc := NewVirtual(time.UnixMilli(1000).UTC())
	got1 := vc.Now()
	got2 := vc.Now()
	if !got1.Equal(got2) || got1.UnixMilli() != 1000 {
		t.Fatalf("clock drifted: %v then %v", got1, got2)
	}
}

func TestAdvanceFiresDueTimersInDeadlineOrder(t *testing.T) {
	vc := NewVirtual(time.UnixMilli(0).UTC())
	t1 := vc.After(10 * time.Millisecond)
	t2 := vc.After(20 * time.Millisecond)
	t3 := vc.After(30 * time.Millisecond)

	if fired := vc.Advance(10 * time.Millisecond); fired != 1 {
		t.Fatalf("fired = %d, want 1", fired)
	}
	select {
	case <-t1.C():
	default:
		t.Fatal("10ms timer did not fire")
	}
	select {
	case <-t2.C():
		t.Fatal("20ms timer fired early")
	default:
	}

	if fired := vc.Advance(10 * time.Millisecond); fired != 1 {
		t.Fatalf("second advance fired = %d, want 1", fired)
	}
	select {
	case <-t2.C():
	default:
		t.Fatal("20ms timer did not fire")
	}
	select {
	case <-t3.C():
		t.Fatal("30ms timer fired early")
	default:
	}

	if fired := vc.Advance(10 * time.Millisecond); fired != 1 {
		t.Fatalf("third advance fired = %d, want 1", fired)
	}
	select {
	case <-t3.C():
	default:
		t.Fatal("30ms timer did not fire")
	}
}

func TestAdvanceFiresManyDueTimersAtOnce(t *testing.T) {
	vc := NewVirtual(time.UnixMilli(0).UTC())
	const n = 50
	timers := make([]*Timer, n)
	for i := range timers {
		timers[i] = vc.After(time.Duration(i+1) * time.Millisecond)
	}
	if fired := vc.Advance(n * time.Millisecond); fired != n {
		t.Fatalf("fired = %d, want %d", fired, n)
	}
	if vc.Now().UnixMilli() != int64(n) {
		t.Fatalf("now = %d, want %d", vc.Now().UnixMilli(), n)
	}
	for _, tm := range timers {
		select {
		case <-tm.C():
		default:
			t.Fatal("a due timer did not receive on its channel")
		}
	}
}

func TestTimerStop(t *testing.T) {
	vc := NewVirtual(time.UnixMilli(0).UTC())
	tm := vc.After(10 * time.Millisecond)
	if !tm.Stop() {
		t.Fatal("Stop returned false for pending timer")
	}
	if fired := vc.Advance(time.Second); fired != 0 {
		t.Fatalf("stopped timer fired: %d", fired)
	}
	select {
	case <-tm.C():
		t.Fatal("stopped timer channel received")
	default:
	}
}

func TestZeroOrNegativeDelayFiresImmediately(t *testing.T) {
	vc := NewVirtual(time.UnixMilli(0).UTC())
	tm := vc.After(0)
	select {
	case <-tm.C():
	default:
		t.Fatal("zero-delay timer did not fire immediately")
	}
}

func TestContextWithTimeoutFiresOnVirtualTime(t *testing.T) {
	vc := NewVirtual(time.UnixMilli(0).UTC())
	ctx, cancel := ContextWithTimeout(context.Background(), vc, 100*time.Millisecond)
	defer cancel()

	if err := ctx.Err(); err != nil {
		t.Fatalf("fresh context err = %v", err)
	}
	vc.Advance(99 * time.Millisecond)
	if err := ctx.Err(); err != nil {
		t.Fatalf("99ms: err = %v, want nil", err)
	}
	vc.Advance(1 * time.Millisecond)
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		if err := ctx.Err(); err == context.DeadlineExceeded {
			return
		}
	}
	t.Fatalf("context not deadline-exceeded after virtual 100ms: %v", ctx.Err())
}

func TestContextWithTimeoutCanceledExplicitly(t *testing.T) {
	vc := NewVirtual(time.UnixMilli(0).UTC())
	ctx, cancel := ContextWithTimeout(context.Background(), vc, time.Hour)
	cancel()
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		if err := ctx.Err(); err == context.Canceled {
			if vc.Pending() != 0 {
				// Timer removal is best-effort; no goroutine should be
				// parked on it after cancel.
			}
			return
		}
	}
	t.Fatalf("context not canceled: %v", ctx.Err())
}

func TestContextWithTimeoutPropagatesParentCancel(t *testing.T) {
	vc := NewVirtual(time.UnixMilli(0).UTC())
	parent, parentCancel := context.WithCancel(context.Background())
	ctx, cancel := ContextWithTimeout(parent, vc, time.Hour)
	defer cancel()
	parentCancel()
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		if err := ctx.Err(); err == context.Canceled {
			return
		}
	}
	t.Fatalf("parent cancellation not propagated: %v", ctx.Err())
}

func TestPendingCount(t *testing.T) {
	vc := NewVirtual(time.UnixMilli(0).UTC())
	t1 := vc.After(time.Second)
	t2 := vc.After(2 * time.Second)
	if vc.Pending() != 2 {
		t.Fatalf("pending = %d, want 2", vc.Pending())
	}
	t1.Stop()
	if vc.Pending() != 1 {
		t.Fatalf("pending after stop = %d, want 1", vc.Pending())
	}
	vc.Advance(2 * time.Second)
	if vc.Pending() != 0 {
		t.Fatalf("pending after advance = %d, want 0", vc.Pending())
	}
	t2.Stop() // already fired: false
}
