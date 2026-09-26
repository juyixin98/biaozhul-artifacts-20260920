package appclient_test

import (
	"context"
	"testing"
	"time"

	"breakerhalfopen/internal/appclient"
	"breakerhalfopen/internal/breaker"
	"breakerhalfopen/internal/clock"
	"breakerhalfopen/internal/upstream"
)

func cfg() breaker.Config {
	return breaker.Config{
		WindowSize: 5, FailureThreshold: 3, OpenCoolDown: 10 * time.Second,
		MaxProbeCalls: 2, HalfOpenSuccessThreshold: 2,
	}
}

func newStack(t *testing.T, script []upstream.Directive, callTimeout time.Duration, healthyFallback bool) (
	*appclient.Client, *breaker.Breaker, *clock.Virtual, *upstream.Upstream,
) {
	t.Helper()
	clk := clock.NewVirtual()
	fallback := upstream.Directive{Fail: !healthyFallback}
	up := upstream.New(clk, fallback, script...)
	brk := breaker.New(cfg(), clk)
	return appclient.New(brk, up, clk, callTimeout), brk, clk, up
}

func TestClassifiesSuccessFailureAndRejected(t *testing.T) {
	cl, brk, _, _ := newStack(t,
		[]upstream.Directive{{}, {Fail: true}, {Fail: true}, {Fail: true}}, 0, true)
	ctx := context.Background()

	if a := cl.Call(ctx); a.Result != appclient.ResultSuccess || a.Generation != 0 || a.Probe {
		t.Fatalf("success attempt=%+v", a)
	}
	for i := 0; i < 3; i++ {
		cl.Call(ctx) // failures #2-4 trip the breaker
	}
	if got := brk.State(); got != breaker.StateOpen {
		t.Fatalf("state=%s want open", got)
	}
	a := cl.Call(ctx)
	if a.Result != appclient.ResultRejected || a.Err == "" {
		t.Fatalf("rejected attempt=%+v", a)
	}
	if len(cl.History()) != 5 {
		t.Fatalf("history len=%d want 5", len(cl.History()))
	}
}

// TestClockDeadlineIsCanceledNotFailure: virtual-time call deadlines classify
// as canceled and never trip the breaker, even repeated far past threshold.
func TestClockDeadlineIsCanceledNotFailure(t *testing.T) {
	cl, brk, clk, up := newStack(t,
		[]upstream.Directive{{Stall: true}, {Stall: true}, {Stall: true}, {Stall: true}},
		100*time.Millisecond, true)

	for i := 0; i < 4; i++ { // 4 > threshold(3): would trip if deadlines counted as failures
		done := make(chan appclient.Attempt, 1)
		go func() { done <- cl.Call(context.Background()) }()
		up.WaitActiveN(1)
		clk.Advance(200 * time.Millisecond) // crosses the 100ms call deadline
		a := <-done
		if a.Result != appclient.ResultCanceled {
			t.Fatalf("attempt %d result=%s want canceled", i, a.Result)
		}
		up.WaitIdle()
	}
	if got := brk.State(); got != breaker.StateClosed {
		t.Fatalf("state=%s want closed (deadlines are not failures)", got)
	}
	s := brk.Snapshot()
	if s.TotalCanceled != 4 || s.TotalFailures != 0 || s.WindowFailures != 0 {
		t.Fatalf("snapshot=%+v", s)
	}
}

// TestCallerCancelIsCanceled: explicit context cancel classifies as canceled.
func TestCallerCancelIsCanceled(t *testing.T) {
	cl, brk, _, up := newStack(t,
		[]upstream.Directive{{Stall: true}}, 0, true)

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan appclient.Attempt, 1)
	go func() { done <- cl.Call(ctx) }()
	up.WaitActiveN(1)
	cancel()
	a := <-done
	if a.Result != appclient.ResultCanceled {
		t.Fatalf("result=%s want canceled", a.Result)
	}
	if got := brk.State(); got != breaker.StateClosed {
		t.Fatalf("state=%s want closed", got)
	}
}

// TestProbeMarkedOnAttempt: attempts admitted in half-open carry Probe=true.
func TestProbeMarkedOnAttempt(t *testing.T) {
	cl, brk, clk, _ := newStack(t,
		[]upstream.Directive{{Fail: true}, {Fail: true}, {Fail: true}}, 0, true)
	ctx := context.Background()
	for i := 0; i < 3; i++ {
		cl.Call(ctx)
	}
	clk.Advance(10 * time.Second)
	if brk.State() != breaker.StateHalfOpen {
		t.Fatalf("state=%s want half_open", brk.State())
	}
	a := cl.Call(ctx)
	if !a.Probe {
		t.Fatalf("attempt in half-open not marked probe: %+v", a)
	}
}
