package upstream

import (
	"context"
	"errors"
	"testing"
	"time"

	"breakerhalfopen/internal/clock"
)

func TestScriptedSuccessFailureAndFallback(t *testing.T) {
	clk := clock.NewVirtual()
	up := New(clk, Directive{Fail: false},
		Directive{}, Directive{Fail: true}, Directive{},
	)
	ctx := context.Background()

	if err := up.Call(ctx); err != nil {
		t.Fatalf("call1: %v", err)
	}
	if err := up.Call(ctx); !errors.Is(err, ErrUpstreamFailure) {
		t.Fatalf("call2 err=%v, want injected failure", err)
	}
	if err := up.Call(ctx); err != nil {
		t.Fatalf("call3 (delayed ok): %v", err)
	}
	// Script exhausted -> fallback (healthy here).
	if err := up.Call(ctx); err != nil {
		t.Fatalf("fallback call: %v", err)
	}
	recs := up.Records()
	if len(recs) != 4 {
		t.Fatalf("records=%d, want 4", len(recs))
	}
	if recs[0].Result != "success" || recs[1].Result != "failure" {
		t.Fatalf("unexpected early records: %+v", recs[:2])
	}
}

func TestStallBlocksUntilReleased(t *testing.T) {
	clk := clock.NewVirtual()
	up := New(clk, Directive{}, Directive{Stall: true, Fail: true})

	done := make(chan error, 1)
	go func() { done <- up.Call(context.Background()) }()
	up.WaitActiveN(1)
	if ids := up.StalledCalls(); len(ids) != 1 {
		t.Fatalf("stalled=%v, want one call", ids)
	}
	select {
	case err := <-done:
		t.Fatalf("stalled call returned early: %v", err)
	default:
	}
	if n := up.ReleaseStalled(); n != 1 {
		t.Fatalf("released=%d, want 1", n)
	}
	select {
	case err := <-done:
		if !errors.Is(err, ErrUpstreamFailure) {
			t.Fatalf("after release err=%v, want injected failure", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("stalled call did not return after release")
	}
}

func TestStallCanceledByContext(t *testing.T) {
	clk := clock.NewVirtual()
	up := New(clk, Directive{}, Directive{Stall: true})
	ctx, cancel := context.WithCancel(context.Background())

	done := make(chan error, 1)
	go func() { done <- up.Call(ctx) }()
	up.WaitActiveN(1)
	cancel()
	select {
	case err := <-done:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("err=%v, want context.Canceled", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("cancel did not unblock the stalled call")
	}
	if recs := up.Records(); len(recs) != 1 || recs[0].Result != "canceled" {
		t.Fatalf("records=%+v, want one canceled", recs)
	}
}

func TestDelayedCallCanceledByClockDrivenDeadline(t *testing.T) {
	clk := clock.NewVirtual()
	up := New(clk, Directive{}, Directive{Delay: time.Hour})

	ctx, cancel := clock.WithTimeout(context.Background(), clk, 100*time.Millisecond)
	defer cancel()

	done := make(chan error, 1)
	go func() { done <- up.Call(ctx) }()
	up.WaitActiveN(1)

	clk.Advance(200 * time.Millisecond) // crosses the 100ms deadline, not the 1h delay
	select {
	case err := <-done:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("err=%v, want context.Canceled", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("clock-driven deadline did not cancel the delayed call")
	}
	up.WaitIdle()
}

func TestDynamicBehaviorOverride(t *testing.T) {
	clk := clock.NewVirtual()
	up := New(clk, Directive{}) // healthy fallback

	up.SetBehavior(&Directive{Fail: true})
	for i := 0; i < 3; i++ {
		if err := up.Call(context.Background()); !errors.Is(err, ErrUpstreamFailure) {
			t.Fatalf("dynamic call %d err=%v, want failure", i, err)
		}
	}
	up.SetBehavior(nil)
	if err := up.Call(context.Background()); err != nil {
		t.Fatalf("after clearing behavior err=%v, want success", err)
	}
}

func TestReleaseOneFreesSingleSlot(t *testing.T) {
	clk := clock.NewVirtual()
	up := New(clk, Directive{}, Directive{Stall: true}, Directive{Stall: true})

	d1 := make(chan error, 1)
	d2 := make(chan error, 1)
	go func() { d1 <- up.Call(context.Background()) }()
	go func() { d2 <- up.Call(context.Background()) }()
	up.WaitActiveN(2)

	if !up.ReleaseOne() {
		t.Fatal("ReleaseOne returned false")
	}
	finished := 0
	wait := func(ch chan error) bool {
		select {
		case <-ch:
			return true
		case <-time.After(50 * time.Millisecond):
			return false
		}
	}
	if wait(d1) {
		finished++
	}
	if wait(d2) {
		finished++
	}
	if finished != 1 {
		t.Fatalf("finished=%d, want exactly 1", finished)
	}
	if up.ActiveCalls() != 1 {
		t.Fatalf("active=%d, want 1", up.ActiveCalls())
	}
	up.ReleaseStalled()
	up.WaitIdle()
}
