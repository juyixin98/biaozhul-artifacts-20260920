package clock

import (
	"context"
	"testing"
	"time"
)

func TestFakeTimerFiresInOrder(t *testing.T) {
	clk := NewFakeClockAt(time.Unix(0, 0))
	t1 := clk.NewTimer(10 * time.Millisecond)
	t2 := clk.NewTimer(5 * time.Millisecond)
	t3 := clk.NewTimer(5 * time.Millisecond)

	base := time.Unix(0, 0)
	if !clk.FireNext(base.Add(100 * time.Millisecond)) {
		t.Fatal("expected first fire")
	}
	got := <-t2.C()
	if got.UnixMilli() != 5 {
		t.Fatalf("first timer at %d want 5", got.UnixMilli())
	}
	if !clk.FireNext(base.Add(100 * time.Millisecond)) {
		t.Fatal("expected second fire (FIFO tie)")
	}
	<-t3.C()
	if !clk.FireNext(base.Add(100 * time.Millisecond)) {
		t.Fatal("expected third fire")
	}
	<-t1.C()
	if clk.FireNext(base.Add(100 * time.Millisecond)) {
		t.Fatal("no timers should remain")
	}
}

func TestFakeTimerStop(t *testing.T) {
	clk := NewFakeClock()
	tm := clk.NewTimer(10 * time.Millisecond)
	if !tm.Stop() {
		t.Fatal("first Stop should report true")
	}
	if tm.Stop() {
		t.Fatal("second Stop should report false")
	}
	if clk.FireNext(clk.Now().Add(time.Hour)) {
		t.Fatal("stopped timer must not fire")
	}
}

func TestDeadlineContext(t *testing.T) {
	clk := NewFakeClock()
	ctx, stop := clk.DeadlineContext(context.Background(), clk.Now().Add(10*time.Millisecond))
	if err := ctx.Err(); err != nil {
		t.Fatalf("fresh ctx err=%v", err)
	}
	clk.FireNext(clk.Now().Add(10 * time.Millisecond))
	<-ctx.Done()
	if ctx.Err() != context.DeadlineExceeded {
		t.Fatalf("err=%v want DeadlineExceeded", ctx.Err())
	}
	if cause := context.Cause(ctx); cause != context.DeadlineExceeded {
		t.Fatalf("cause=%v", cause)
	}
	stop() // idempotent cleanup must not panic
}

func TestDeadlineContextExplicitCancel(t *testing.T) {
	clk := NewFakeClock()
	ctx, stop := clk.DeadlineContext(context.Background(), clk.Now().Add(time.Hour))
	stop()
	<-ctx.Done()
	if ctx.Err() != context.Canceled {
		t.Fatalf("err=%v want Canceled", ctx.Err())
	}
}

func TestParentCancelPropagates(t *testing.T) {
	clk := NewFakeClock()
	parent, cancelParent := context.WithCancel(context.Background())
	ctx, _ := clk.DeadlineContext(parent, clk.Now().Add(time.Hour))
	cancelParent()
	<-ctx.Done()
	if ctx.Err() != context.Canceled {
		t.Fatalf("err=%v want Canceled", ctx.Err())
	}
}

func TestSchedulerTimerIsolated(t *testing.T) {
	clk := NewFakeClock()
	ord := clk.NewTimer(5 * time.Millisecond)
	sch := clk.NewSchedulerTimer(2 * time.Millisecond)
	until := clk.Now().Add(time.Hour)
	// Ordinary FireNext ignores the earlier scheduler timer.
	if !clk.FireNext(until) {
		t.Fatal("ordinary timer should fire")
	}
	select {
	case <-ord.C():
	default:
		t.Fatal("ordinary timer did not deliver")
	}
	select {
	case <-sch.C():
		t.Fatal("scheduler timer must not fire via FireNext")
	default:
	}
	if !clk.FireSchedulerWake(until) {
		t.Fatal("scheduler timer should fire via FireSchedulerWake")
	}
	<-sch.C()
}
