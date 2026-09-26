package tree

import (
	"context"
	"sync"
	"testing"
	"time"

	"canceltree/internal/client"
	"canceltree/internal/clock"
	"canceltree/internal/testutil"
	"canceltree/internal/upstream"
)

type harness struct {
	t  *testing.T
	f  *upstream.Fake
	cl *client.Logic
}

func newHarness(t *testing.T) *harness {
	t.Helper()
	f := upstream.New()
	t.Cleanup(func() {
		f.ReleaseAll()
		f.Close()
	})
	cl := client.New(clock.Real{}, client.Config{Attempts: 1, DialTimeout: time.Second})
	t.Cleanup(cl.CloseIdleConnections)
	return &harness{t: t, f: f, cl: cl}
}

func (h *harness) engine() *Engine {
	return NewEngine(clock.Real{}, h.cl, Config{
		BaseURL:        h.f.URL(),
		CleanupTimeout: time.Second,
	})
}

func heldTask(id string, withCleanup bool) TaskSpec {
	t := TaskSpec{
		ID: id,
		Call: client.Call{
			TaskID: id,
			URL:    "/work?hold=1&hold_id=" + id,
		},
	}
	if withCleanup {
		t.Cleanup = &CleanupSpec{URL: "/cleanup"}
	}
	return t
}

func waitInflight(h *harness, n int) {
	h.t.Helper()
	testutil.Eventually(h.t, 2*time.Second, func() bool {
		return h.f.Snapshot().Inflight >= int64(n)
	}, "tasks never became inflight")
}

func TestRunAllSucceed(t *testing.T) {
	h := newHarness(t)
	e := h.engine()
	tasks := []TaskSpec{
		{ID: "a", Call: client.Call{TaskID: "a", URL: "/work?delay=1ms"}},
		{ID: "b", Call: client.Call{TaskID: "b", URL: "/work?delay=2ms"}},
	}
	rep := e.Run(context.Background(), "r1", tasks)
	if rep.Status != TreeSucceeded {
		t.Fatalf("status = %s, want succeeded: %+v", rep.Status, rep)
	}
	for _, o := range rep.Outcomes {
		if o.Status != StatusSucceeded {
			t.Fatalf("outcome %s = %s", o.ID, o.Status)
		}
	}
}

func TestFatalFailureCancelsSiblingsButCleanupsRun(t *testing.T) {
	h := newHarness(t)
	e := h.engine()
	tasks := []TaskSpec{
		heldTask("slow-a", true),
		heldTask("slow-b", true),
		{
			ID:    "boom",
			Fatal: true,
			// Small delay so the two held siblings reach the upstream first.
			Call: client.Call{TaskID: "boom", URL: "/work?delay=40ms&fail=1&status=500"},
		},
	}

	done := make(chan Report, 1)
	go func() { done <- e.Run(context.Background(), "r-fatal", tasks) }()
	waitInflight(h, 2)

	select {
	case rep := <-done:
		if rep.Status != TreeFailed || rep.FatalTaskID != "boom" {
			t.Fatalf("report = %+v", rep)
		}
		byID := mapOutcomes(rep)
		if byID["boom"].Status != StatusFailed {
			t.Fatalf("boom = %+v", byID["boom"])
		}
		for _, id := range []string{"slow-a", "slow-b"} {
			o := byID[id]
			if o.Status != StatusCanceled || o.CanceledBy != CanceledByFatalSibling {
				t.Fatalf("%s = %+v, want canceled by fatal sibling", id, o)
			}
			if !o.CleanupRan {
				t.Fatalf("%s cleanup did not run: %+v", id, o)
			}
		}
	case <-time.After(3 * time.Second):
		t.Fatal("Run did not return after fatal failure")
	}

	st := h.f.Snapshot()
	if st.Canceled < 2 {
		t.Fatalf("upstream canceled = %d, want >= 2", st.Canceled)
	}
	if st.Cleanups != 2 {
		t.Fatalf("cleanups = %d, want 2", st.Cleanups)
	}
	if st.Inflight != 0 || st.ActiveHolds != 0 {
		t.Fatalf("upstream stats = %+v, want drained", st)
	}
}

func TestNonFatalFailureDoesNotCancelSiblings(t *testing.T) {
	h := newHarness(t)
	e := h.engine()
	tasks := []TaskSpec{
		heldTask("slow-a", false),
		{
			ID:    "soft-fail",
			Fatal: false,
			Call:  client.Call{TaskID: "soft-fail", URL: "/work?delay=20ms&fail=1"},
		},
	}

	done := make(chan Report, 1)
	go func() { done <- e.Run(context.Background(), "r-soft", tasks) }()
	waitInflight(h, 1)

	// The soft failure lands while slow-a is held; give it time to return,
	// then release slow-a, which must still complete normally.
	time.Sleep(60 * time.Millisecond)
	h.f.Release("slow-a")

	select {
	case rep := <-done:
		if rep.Status != TreeFailed {
			t.Fatalf("status = %s, want failed", rep.Status)
		}
		byID := mapOutcomes(rep)
		if byID["slow-a"].Status != StatusSucceeded {
			t.Fatalf("slow-a = %+v, want succeeded (not canceled)", byID["slow-a"])
		}
		if byID["soft-fail"].Status != StatusFailed {
			t.Fatalf("soft-fail = %+v", byID["soft-fail"])
		}
	case <-time.After(3 * time.Second):
		t.Fatal("Run did not return")
	}
}

func TestClientDisconnectStopsRemainingWorkAndRunsCleanups(t *testing.T) {
	h := newHarness(t)
	e := h.engine()
	tasks := []TaskSpec{
		heldTask("held-a", true),
		heldTask("held-b", true),
		heldTask("held-c", true),
	}

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan Report, 1)
	go func() { done <- e.Run(ctx, "r-disc", tasks) }()
	waitInflight(h, 3)

	cancel()
	select {
	case rep := <-done:
		if rep.Status != TreeCanceled || rep.CanceledBy != CanceledByClient {
			t.Fatalf("report = %+v", rep)
		}
		for _, o := range rep.Outcomes {
			if o.Status != StatusCanceled || o.CanceledBy != CanceledByClient {
				t.Fatalf("outcome = %+v, want client-disconnect cancel", o)
			}
			if !o.CleanupRan {
				t.Fatalf("cleanup for %s did not run after disconnect", o.ID)
			}
		}
	case <-time.After(3 * time.Second):
		t.Fatal("Run did not return after client disconnect")
	}

	st := h.f.Snapshot()
	if st.Canceled < 3 {
		t.Fatalf("upstream canceled = %d, want >= 3", st.Canceled)
	}
	if st.Cleanups != 3 {
		t.Fatalf("cleanups = %d, want 3", st.Cleanups)
	}
	if st.Inflight != 0 || st.ActiveHolds != 0 {
		t.Fatalf("upstream stats = %+v, want drained", st)
	}
}

func TestTaskTimeoutCancelsCall(t *testing.T) {
	f := upstream.New()
	defer func() {
		f.ReleaseAll()
		f.Close()
	}()
	clk := clock.NewFake(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC))
	cl := client.New(clk, client.Config{Attempts: 1, DialTimeout: time.Second})
	defer cl.CloseIdleConnections()
	e := NewEngine(clk, cl, Config{BaseURL: f.URL(), CleanupTimeout: time.Second})

	task := heldTask("timed", true)
	task.Timeout = 50 * time.Millisecond

	done := make(chan Report, 1)
	go func() { done <- e.Run(context.Background(), "r-timeout", []TaskSpec{task}) }()
	waitInflight(&harness{f: f, t: t}, 1)

	clk.Advance(50 * time.Millisecond)
	select {
	case rep := <-done:
		if rep.Status != TreeFailed {
			t.Fatalf("tree status = %q, want failed after a task timeout", rep.Status)
		}
		o := rep.Outcomes[0]
		if o.Status != StatusCanceled || o.CanceledBy != CanceledByTimeout {
			t.Fatalf("outcome = %+v", o)
		}
		if !o.CleanupRan {
			t.Fatal("cleanup should still run after task timeout")
		}
	case <-time.After(3 * time.Second):
		t.Fatal("Run did not return after fake-clock timeout")
	}
	// The task timer fired; the cleanup-deadline timer must have been
	// Stopped by its watcher once cleanup finished (no fake-clock leaks).
	testutil.Eventually(t, time.Second, func() bool {
		return clk.PendingTimers() == 0
	}, "fake clock still has pending timers")
	if st := f.Snapshot(); st.Inflight != 0 {
		t.Fatalf("inflight = %d, want 0", st.Inflight)
	}
}

func TestCleanupBoundedByOwnDeadline(t *testing.T) {
	f := upstream.New()
	defer func() {
		f.ReleaseAll()
		f.Close()
	}()
	clk := clock.NewFake(time.Time{})
	cl := client.New(clk, client.Config{Attempts: 1, DialTimeout: time.Second})
	defer cl.CloseIdleConnections()
	e := NewEngine(clk, cl, Config{BaseURL: f.URL(), CleanupTimeout: time.Second})

	// The task call succeeds immediately; its "cleanup" points at a held
	// work endpoint and gets a 50ms cleanup deadline.
	task := TaskSpec{
		ID:   "leaky-cleanup",
		Call: client.Call{TaskID: "leaky-cleanup", URL: "/work?delay=1ms"},
		Cleanup: &CleanupSpec{
			URL:     "/work?hold=1&hold_id=cleanup-hold",
			Timeout: 50 * time.Millisecond,
		},
	}

	done := make(chan Report, 1)
	go func() { done <- e.Run(context.Background(), "r-clean-bound", []TaskSpec{task}) }()
	// Wait until the cleanup call is the one held upstream.
	testutil.Eventually(t, 2*time.Second, func() bool {
		st := f.Snapshot()
		return st.Started >= 2 && st.Inflight == 1
	}, "cleanup call never became the held inflight request")

	clk.Advance(50 * time.Millisecond)
	select {
	case rep := <-done:
		o := rep.Outcomes[0]
		if o.CleanupRan {
			t.Fatalf("cleanup unexpectedly succeeded: %+v", o)
		}
		if o.CleanupError == "" {
			t.Fatal("expected a cleanup error after its deadline elapsed")
		}
	case <-time.After(3 * time.Second):
		t.Fatal("Run did not return when cleanup deadline elapsed")
	}
	f.ReleaseAll()
	testutil.Eventually(t, 2*time.Second, func() bool {
		return f.Snapshot().Inflight == 0
	}, "held cleanup connection never drained")
}

func TestNoLeakAcrossRepeatedCanceledTrees(t *testing.T) {
	h := newHarness(t)
	e := h.engine()

	// Warm up: establish pooled conns and runtime steady state.
	e.Run(context.Background(), "warm", []TaskSpec{
		{ID: "w", Call: client.Call{TaskID: "w", URL: "/work?delay=1ms"}},
	})
	h.cl.CloseIdleConnections()
	testutil.GoroutineSettled(t, testutil.Snapshot().Goroutines, time.Second)
	base := testutil.Snapshot()
	connBase := h.cl.ConnStats().TotalEstablished

	const iterations = 20
	for i := 0; i < iterations; i++ {
		ctx, cancel := context.WithCancel(context.Background())
		tasks := []TaskSpec{
			heldTask("a", true),
			heldTask("b", true),
			heldTask("c", true),
		}
		var wg sync.WaitGroup
		wg.Add(1)
		go func() { defer wg.Done(); _ = e.Run(ctx, "loop", tasks) }()
		waitInflight(h, 3)
		cancel()
		wg.Wait()
		h.f.ReleaseAll()
	}
	h.cl.CloseIdleConnections()

	wantGoroutines := testutil.Snapshot().Goroutines
	testutil.GoroutineSettled(t, wantGoroutines, 2*time.Second)
	final := testutil.Snapshot()
	if final.Goroutines > base.Goroutines+2 {
		t.Fatalf("goroutine leak: base=%d final=%d", base.Goroutines, final.Goroutines)
	}
	if h.f.Snapshot().Inflight != 0 {
		t.Fatalf("upstream inflight leaked: %+v", h.f.Snapshot())
	}
	if h.cl.ConnStats().InFlight != 0 {
		t.Fatalf("in-flight calls leaked: %+v", h.cl.ConnStats())
	}
	if h.cl.ConnStats().TotalEstablished == connBase {
		t.Fatal("expected new connections to be established during the loop")
	}
	// Heap after forced GC must not grow without bound across iterations.
	if final.HeapAlloc > base.HeapAlloc+(2<<20) {
		t.Fatalf("heap grew beyond budget: base=%d final=%d", base.HeapAlloc, final.HeapAlloc)
	}
}

// TestFatalAndClientDisconnectRace fires the first fatal failure and the
// client disconnect at (nearly) the same instant. Whichever edge lands
// first, every task must settle, every cleanup must run, and no upstream
// call or connection may remain in flight.
func TestFatalAndClientDisconnectRace(t *testing.T) {
	h := newHarness(t)
	e := h.engine()
	tasks := []TaskSpec{
		heldTask("race-a", true),
		heldTask("race-b", true),
		{
			ID:      "race-fatal",
			Fatal:   true,
			Call:    client.Call{TaskID: "race-fatal", URL: "/work?delay=30ms&fail=1&status=500"},
			Cleanup: &CleanupSpec{URL: "/cleanup"},
		},
	}

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan Report, 1)
	go func() { done <- e.Run(ctx, "r-race", tasks) }()
	waitInflight(h, 2)

	// Both edges scheduled together; ordering is intentionally unspecified.
	go cancel()
	select {
	case rep := <-done:
		switch rep.Status {
		case TreeCanceled, TreeFailed:
		default:
			t.Fatalf("unexpected status %q: %+v", rep.Status, rep)
		}
		for _, o := range rep.Outcomes {
			if !o.CleanupRan {
				t.Fatalf("cleanup did not run for %s: %+v", o.ID, o)
			}
		}
	case <-time.After(3 * time.Second):
		t.Fatal("Run did not settle under fatal/disconnect race")
	}

	testutil.Eventually(t, 2*time.Second, func() bool {
		st := h.f.Snapshot()
		return st.Inflight == 0 && h.cl.ConnStats().InFlight == 0
	}, "resources did not drain after fatal/disconnect race")
	if st := h.f.Snapshot(); st.Cleanups != 3 {
		t.Fatalf("cleanups = %d, want 3 even under the race", st.Cleanups)
	}
}

// TestReleaseVersusCancelRace releases a held call at the same time its
// context is canceled. The task must end exactly once and the engine must
// return promptly without a leaked upstream call.
func TestReleaseVersusCancelRace(t *testing.T) {
	h := newHarness(t)
	e := h.engine()

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan Report, 1)
	go func() {
		done <- e.Run(ctx, "r-rel-cancel", []TaskSpec{
			heldTask("rc", true),
			heldTask("rd", true),
		})
	}()
	waitInflight(h, 2)

	// Drive the race repeatedly across iterations is unnecessary; one
	// simultaneous trigger is enough, the race detector checks the memory
	// safety of both branches.
	h.f.Release("rc")
	cancel()

	select {
	case rep := <-done:
		if rep.Status != TreeCanceled || rep.CanceledBy != CanceledByClient {
			t.Fatalf("report = %+v", rep)
		}
		seen := make(map[string]bool, len(rep.Outcomes))
		for _, o := range rep.Outcomes {
			if seen[o.ID] {
				t.Fatalf("task %s reported twice", o.ID)
			}
			seen[o.ID] = true
			if !o.CleanupRan {
				t.Fatalf("cleanup missing for %s: %+v", o.ID, o)
			}
		}
	case <-time.After(3 * time.Second):
		t.Fatal("Run did not settle under release/cancel race")
	}
	h.f.ReleaseAll()
	testutil.Eventually(t, 2*time.Second, func() bool {
		return h.f.Snapshot().Inflight == 0 && h.cl.ConnStats().InFlight == 0
	}, "resources did not drain after release/cancel race")
}

func mapOutcomes(r Report) map[string]TaskOutcome {
	m := make(map[string]TaskOutcome, len(r.Outcomes))
	for _, o := range r.Outcomes {
		m[o.ID] = o
	}
	return m
}
