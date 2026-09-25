package executor

import (
	"context"
	"errors"
	"testing"
	"time"

	"deadlineadm/clock"
)

func TestScriptNaturalCompletion(t *testing.T) {
	clk := clock.NewFakeClock()
	ex := NewScriptExecutor(clk)
	ch := ex.Start(context.Background(), "j", "sleep:25")
	clk.FireNext(clk.Now().Add(time.Hour))
	ex.WaitIdle()
	r := <-ch
	if r.Err != nil {
		t.Fatalf("err=%v want nil", r.Err)
	}
	if r.Ran != 25*time.Millisecond {
		t.Fatalf("ran=%v want 25ms", r.Ran)
	}
}

func TestScriptFailure(t *testing.T) {
	clk := clock.NewFakeClock()
	ex := NewScriptExecutor(clk)
	ch := ex.Start(context.Background(), "j", "sleep:5,fail")
	clk.FireNext(clk.Now().Add(time.Hour))
	r := <-ch
	if r.Err == nil {
		t.Fatal("want execution error")
	}
}

func TestImmediateFailure(t *testing.T) {
	ex := NewScriptExecutor(clock.NewFakeClock())
	r := <-ex.Start(context.Background(), "j", "fail:boom")
	if r.Err == nil || r.Err.Error() != "boom" {
		t.Fatalf("err=%v want boom", r.Err)
	}
}

func TestScriptBadPayload(t *testing.T) {
	ex := NewScriptExecutor(clock.NewFakeClock())
	r := <-ex.Start(context.Background(), "j", "nonsense")
	if r.Err == nil {
		t.Fatal("want parse error")
	}
}

func TestKillBeforeBoundReportsKilled(t *testing.T) {
	clk := clock.NewFakeClock()
	ex := NewScriptExecutor(clk)
	ctx, stop := clk.DeadlineContext(context.Background(), clk.Now().Add(10*time.Millisecond))
	defer stop()
	ch := ex.Start(ctx, "j", "sleep:50")
	clk.FireNext(clk.Now().Add(10 * time.Millisecond)) // deadline fires first
	r := <-ch
	if !errors.Is(r.Err, ErrKilled) {
		t.Fatalf("err=%v want ErrKilled", r.Err)
	}
}

func TestExplicitCancelReportsCanceled(t *testing.T) {
	clk := clock.NewFakeClock()
	ex := NewScriptExecutor(clk)
	ctx, cancel := context.WithCancel(context.Background())
	ch := ex.Start(ctx, "j", "sleep:50")
	cancel()
	r := <-ch
	if !errors.Is(r.Err, ErrCanceled) {
		t.Fatalf("err=%v want ErrCanceled", r.Err)
	}
}

func TestCompletionBeforeKillWins(t *testing.T) {
	clk := clock.NewFakeClock()
	ex := NewScriptExecutor(clk)
	// Sleep ends at t=30; the kill bound is later at t=40. The scheduler
	// guarantees the completion is observed before the bound (its wake heap
	// dispatches the earlier event first), and here the executor must report
	// natural success rather than a kill.
	ctx, stop := clk.DeadlineContext(context.Background(), clk.Now().Add(40*time.Millisecond))
	defer stop()
	ch := ex.Start(ctx, "j", "sleep:30")
	clk.FireNext(clk.Now().Add(time.Hour)) // t=30 completion
	ex.WaitIdle()
	r := <-ch
	if r.Err != nil {
		t.Fatalf("job completing before its bound must succeed, got err=%v", r.Err)
	}
	if r.Ran != 30*time.Millisecond {
		t.Fatalf("ran=%v want 30ms", r.Ran)
	}
}

func TestZeroDurationRunsImmediately(t *testing.T) {
	ex := NewScriptExecutor(clock.NewFakeClock())
	r := <-ex.Start(context.Background(), "j", "sleep:0,fail")
	if r.Err == nil {
		t.Fatal("sleep:0,fail must return its failure without waiting for a timer")
	}
}
