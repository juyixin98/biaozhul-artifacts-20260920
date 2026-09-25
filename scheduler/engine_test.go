package scheduler

import (
	"fmt"
	"sync"
	"testing"
	"time"
)

// ---------------------------------------------------------------------------
// Cycle detection
// ---------------------------------------------------------------------------

func TestRejectsSelfCycle(t *testing.T) {
	eng := New(newScriptedExec())
	_, err := eng.Submit(&DagSpec{Nodes: []NodeSpec{
		{Name: "A", Deps: []string{"A"}},
	}})
	if err == nil {
		t.Fatal("expected cycle error")
	}
}

func TestRejectsDirectedCycle(t *testing.T) {
	ex := newScriptedExec()
	eng := newTestEngine(t, ex, nil)
	_, err := eng.Submit(&DagSpec{Nodes: []NodeSpec{
		{Name: "A", Deps: []string{"C"}},
		{Name: "B", Deps: []string{"A"}},
		{Name: "C", Deps: []string{"B"}},
	}})
	if err == nil {
		t.Fatal("expected cycle error, got nil")
	}
	// Executor must never have been touched.
	if ex.total != 0 {
		t.Fatalf("executor ran %d times despite cycle", ex.total)
	}
}

func TestRejectsUnknownDep(t *testing.T) {
	eng := New(newScriptedExec())
	_, err := eng.Submit(&DagSpec{Nodes: []NodeSpec{
		{Name: "A", Deps: []string{"ghost"}},
	}})
	if err == nil {
		t.Fatal("expected unknown-dependency error")
	}
}

// ---------------------------------------------------------------------------
// Failure propagation: skipped vs failed
// ---------------------------------------------------------------------------

func TestAllSuccessPropagatesSkip(t *testing.T) {
	ex := newScriptedExec()
	ex.alwaysFail("B")
	eng := newTestEngine(t, ex, NewSliceSink())
	// A ok -> B fails (1 attempt) -> C skipped -> D skipped
	job, err := eng.Submit(&DagSpec{
		Name: "diamond-fail",
		Nodes: []NodeSpec{
			{Name: "A", MaxAttempts: 1},
			{Name: "B", Deps: []string{"A"}, MaxAttempts: 1},
			{Name: "C", Deps: []string{"A"}, MaxAttempts: 1},
			{Name: "D", Deps: []string{"B", "C"}, MaxAttempts: 1},
		},
	})
	if err != nil {
		t.Fatalf("submit: %v", err)
	}
	job.Wait()

	if job.State() != JobFailed {
		t.Fatalf("job state = %s, want FAILED", job.State())
	}
	snap := job.Snapshot()
	wantStates := map[string]NodeState{
		"A": NodeSucceeded,
		"B": NodeFailed,
		"C": NodeSucceeded,
		"D": NodeSkipped,
	}
	for _, n := range snap.Nodes {
		if want := wantStates[n.Name]; n.State != want {
			t.Errorf("node %s state=%s want %s", n.Name, n.State, want)
		}
	}
	// D must never execute.
	if ex.callCount("D") != 0 {
		t.Errorf("skipped node D executed %d times", ex.callCount("D"))
	}
}

func TestAllEndRunsDespiteFailedDep(t *testing.T) {
	ex := newScriptedExec()
	ex.alwaysFail("B")
	eng := newTestEngine(t, ex, nil)
	job, err := eng.Submit(&DagSpec{
		Name: "all-end",
		Nodes: []NodeSpec{
			{Name: "A", MaxAttempts: 1},
			{Name: "B", Deps: []string{"A"}, Policy: RequireAllEnd, MaxAttempts: 1},
			{Name: "C", Deps: []string{"A"}, Policy: RequireAllEnd, MaxAttempts: 1},
			{Name: "D", Deps: []string{"B", "C"}, Policy: RequireAllEnd, MaxAttempts: 1},
		},
	})
	if err != nil {
		t.Fatalf("submit: %v", err)
	}
	job.Wait()

	// D runs because ALL_END waits for termination, not success.
	if ex.callCount("D") != 1 {
		t.Fatalf("D calls=%d, want 1 (ALL_END must run it)", ex.callCount("D"))
	}
	if job.State() != JobFailed {
		t.Fatalf("job state=%s, want FAILED because B genuinely failed", job.State())
	}
	states := stateMap(job)
	if states["D"] != NodeSucceeded {
		t.Errorf("D state=%s want SUCCEEDED", states["D"])
	}
}

func TestAllSuccessSkipChain(t *testing.T) {
	// Failed A skips B, and a downstream ALL_SUCCESS node of skipped B also skips.
	ex := newScriptedExec()
	ex.alwaysFail("A")
	eng := newTestEngine(t, ex, nil)
	job, _ := eng.Submit(&DagSpec{Nodes: []NodeSpec{
		{Name: "A", MaxAttempts: 1},
		{Name: "B", Deps: []string{"A"}, MaxAttempts: 1},
		{Name: "C", Deps: []string{"B"}, MaxAttempts: 1},
	}})
	job.Wait()
	states := stateMap(job)
	if states["A"] != NodeFailed || states["B"] != NodeSkipped || states["C"] != NodeSkipped {
		t.Fatalf("states=%v want A:FAILED B:SKIPPED C:SKIPPED", states)
	}
	if ex.callCount("B")+ex.callCount("C") != 0 {
		t.Fatal("skipped nodes must not execute")
	}
}

// ---------------------------------------------------------------------------
// Retry budget
// ---------------------------------------------------------------------------

func TestRetryThenSucceed(t *testing.T) {
	ex := newScriptedExec()
	ex.failTimes("A", 2, "transient")
	sink := NewSliceSink()
	eng := newTestEngine(t, ex, sink)
	job, _ := eng.Submit(&DagSpec{Nodes: []NodeSpec{
		{Name: "A", MaxAttempts: 3},
	}})
	job.Wait()

	if got := ex.attemptsOf("A"); fmt.Sprint(got) != "[1 2 3]" {
		t.Errorf("attempts=%v want [1 2 3]", got)
	}
	if job.State() != JobSucceeded {
		t.Fatalf("state=%s want SUCCEEDED", job.State())
	}
	events := sink.EventsFor(job.ID)
	var retries int
	for _, e := range events {
		if e.Type == EventNodeRetry {
			retries++
			if e.Backoff <= 0 {
				t.Errorf("retry event backoff=%v want >0", e.Backoff)
			}
		}
	}
	if retries != 2 {
		t.Errorf("retry events=%d want 2", retries)
	}
}

func TestRetryBudgetExhausted(t *testing.T) {
	ex := newScriptedExec()
	ex.alwaysFail("A")
	eng := newTestEngine(t, ex, nil)
	job, _ := eng.Submit(&DagSpec{Nodes: []NodeSpec{
		{Name: "A", MaxAttempts: 3},
	}})
	job.Wait()
	if ex.callCount("A") != 3 {
		t.Errorf("calls=%d want exactly 3", ex.callCount("A"))
	}
	if stateMap(job)["A"] != NodeFailed {
		t.Error("A should be FAILED")
	}
}

// ---------------------------------------------------------------------------
// No duplicate concurrent execution — the core acceptance invariant.
// ---------------------------------------------------------------------------

func TestNoDuplicateConcurrentExecution(t *testing.T) {
	ex := newScriptedExec()
	var gate sync.WaitGroup
	gate.Add(1)
	started := make(chan struct{}, 8)
	ex.on("A", func(_ *ExecContext, _ int) error {
		started <- struct{}{}
		gate.Wait() // block until released
		return nil
	})
	eng := New(ex, WithBackoff(func(int) time.Duration { return 0 }))
	job, _ := eng.Submit(&DagSpec{Nodes: []NodeSpec{
		{Name: "A", MaxAttempts: 5},
	}})

	<-started
	// Give any buggy double-launch room to surface.
	time.Sleep(30 * time.Millisecond)
	ex.mu.Lock()
	concurrent := ex.maxSeen["A"]
	calls := len(ex.calls["A"])
	ex.mu.Unlock()
	if concurrent != 1 || calls != 1 {
		t.Errorf("during blocked run: concurrent=%d calls=%d, want 1/1", concurrent, calls)
	}
	gate.Done()
	job.Wait()
	if ex.maxConcurrent("A") != 1 {
		t.Errorf("max concurrent executions of A = %d, want 1", ex.maxConcurrent("A"))
	}
	if ex.callCount("A") != 1 {
		t.Errorf("total calls A=%d want 1", ex.callCount("A"))
	}
}

func TestNoConcurrentAcrossRetriesWithFakeClock(t *testing.T) {
	// Retry backoff: attempt N fully ends before N+1 starts. FakeClock proves
	// a waiting retry cannot overlap the next attempt.
	ex := newScriptedExec()
	ex.failTimes("A", 2, "boom")
	clk := NewFakeClock(time.Unix(1, 0))
	eng := New(ex,
		WithClock(clk),
		WithBackoff(func(int) time.Duration { return 50 * time.Millisecond }),
	)
	job, _ := eng.Submit(&DagSpec{Nodes: []NodeSpec{{Name: "A", MaxAttempts: 3}}})

	// Attempt 1 fails fast; retry wait registered with the fake clock.
	waitFor(t, func() bool { return clk.PeekWaiters() == 1 }, time.Second)
	ex.mu.Lock()
	calls := len(ex.calls["A"])
	ex.mu.Unlock()
	if calls != 1 {
		t.Fatalf("before advancing clock calls=%d want 1", calls)
	}
	clk.Advance(50 * time.Millisecond)
	waitFor(t, func() bool {
		ex.mu.Lock()
		defer ex.mu.Unlock()
		return len(ex.calls["A"]) == 2
	}, time.Second)
	waitFor(t, func() bool { return clk.PeekWaiters() == 1 }, time.Second)
	clk.Advance(50 * time.Millisecond)
	job.Wait()
	if ex.maxConcurrent("A") != 1 {
		t.Errorf("max concurrent = %d, want 1", ex.maxConcurrent("A"))
	}
	if ex.callCount("A") != 3 {
		t.Errorf("calls=%d want 3", ex.callCount("A"))
	}
}

// ---------------------------------------------------------------------------
// Cancellation
// ---------------------------------------------------------------------------

func TestCancelStopsRunningNode(t *testing.T) {
	ex := newScriptedExec()
	release := make(chan struct{})
	var sawCancel bool
	var cancelMu sync.Mutex
	ex.on("slow", func(ec *ExecContext, _ int) error {
		<-ec.Ctx.Done()
		cancelMu.Lock()
		sawCancel = true
		cancelMu.Unlock()
		return fmt.Errorf("aborted: %w", ec.Ctx.Err())
	})
	ex.on("after", func(*ExecContext, int) error { return nil })
	eng := newTestEngine(t, ex, nil)
	job, _ := eng.Submit(&DagSpec{Nodes: []NodeSpec{
		{Name: "slow", MaxAttempts: 1},
		{Name: "after", Deps: []string{"slow"}, MaxAttempts: 1},
	}})

	waitFor(t, func() bool { return ex.callCount("slow") == 1 }, time.Second)
	if !job.Cancel() {
		t.Fatal("cancel rejected")
	}
	job.Wait()

	if job.State() != JobCanceled {
		t.Fatalf("state=%s want CANCELED", job.State())
	}
	cancelMu.Lock()
	sc := sawCancel
	cancelMu.Unlock()
	if !sc {
		t.Error("running node did not observe context cancellation")
	}
	if ex.callCount("after") != 0 {
		t.Errorf("downstream node ran %d times after cancel", ex.callCount("after"))
	}
	if ex.maxConcurrent("slow") != 1 {
		t.Errorf("slow ran concurrently %d times", ex.maxConcurrent("slow"))
	}
	close(release)
}

func TestCancelBeforeNodeStartsSkipsIt(t *testing.T) {
	ex := newScriptedExec()
	releaseA := make(chan struct{})
	ex.on("A", func(ec *ExecContext, _ int) error {
		select {
		case <-releaseA:
			return nil
		case <-ec.Ctx.Done():
			return ec.Ctx.Err()
		}
	})
	eng := newTestEngine(t, ex, nil)
	job, _ := eng.Submit(&DagSpec{Nodes: []NodeSpec{
		{Name: "A", MaxAttempts: 1},
		{Name: "B", Deps: []string{"A"}, MaxAttempts: 1},
	}})
	waitFor(t, func() bool { return ex.callCount("A") == 1 }, time.Second)
	job.Cancel()
	close(releaseA)
	job.Wait()
	states := stateMap(job)
	if states["B"] != NodeSkipped {
		t.Errorf("B=%s want SKIPPED (never started after cancel)", states["B"])
	}
	if ex.callCount("B") != 0 {
		t.Errorf("B executed %d times after cancel", ex.callCount("B"))
	}
}

func TestCancelDuringRetryBackoff(t *testing.T) {
	ex := newScriptedExec()
	ex.alwaysFail("A")
	clk := NewFakeClock(time.Unix(1, 0))
	eng := New(ex,
		WithClock(clk),
		WithBackoff(func(int) time.Duration { return time.Hour }),
	)
	job, _ := eng.Submit(&DagSpec{Nodes: []NodeSpec{{Name: "A", MaxAttempts: 5}}})
	waitFor(t, func() bool { return clk.PeekWaiters() == 1 }, time.Second)
	calls := ex.callCount("A")
	if calls != 1 {
		t.Fatalf("calls before cancel=%d want 1", calls)
	}
	job.Cancel()
	job.Wait()
	// Without advancing the clock, no further attempt may start.
	if ex.callCount("A") != 1 {
		t.Errorf("calls after cancel=%d want 1 (backoff must abort)", ex.callCount("A"))
	}
	if stateMap(job)["A"] != NodeFailed {
		t.Errorf("A=%s want FAILED after aborted backoff", stateMap(job)["A"])
	}
	if job.State() != JobCanceled {
		t.Errorf("job=%s want CANCELED", job.State())
	}
}

// ---------------------------------------------------------------------------
// Events
// ---------------------------------------------------------------------------

func TestEventSequenceAndContent(t *testing.T) {
	ex := newScriptedExec()
	ex.failTimes("B", 1, "nope")
	sink := NewSliceSink()
	eng := New(ex, WithSink(sink), WithBackoff(func(int) time.Duration { return time.Millisecond }))
	job, _ := eng.Submit(&DagSpec{Nodes: []NodeSpec{
		{Name: "A", MaxAttempts: 1},
		{Name: "B", Deps: []string{"A"}, MaxAttempts: 2},
	}})
	job.Wait()

	events := sink.EventsFor(job.ID)
	if len(events) < 6 {
		t.Fatalf("too few events: %d", len(events))
	}
	for i, e := range events {
		if e.Seq != i+1 {
			t.Fatalf("event seq gap: pos %d seq %d", i, e.Seq)
		}
		if e.JobID != job.ID {
			t.Fatal("event missing job id")
		}
		if e.Time.IsZero() {
			t.Fatal("event missing timestamp")
		}
	}
	if events[0].Type != EventJobSubmitted || events[1].Type != EventJobStarted {
		t.Errorf("event prefix=%s,%s", events[0].Type, events[1].Type)
	}
	if last := events[len(events)-1].Type; last != EventJobSucceeded {
		t.Errorf("last event=%s want JOB_SUCCEEDED", last)
	}
	// Retry event carries the error message.
	var sawRetry bool
	for _, e := range events {
		if e.Type == EventNodeRetry && e.Error == "nope on attempt 1" {
			sawRetry = true
		}
	}
	if !sawRetry {
		t.Error("retry event with error not found")
	}
}

// ---------------------------------------------------------------------------
// Multiple independent jobs
// ---------------------------------------------------------------------------

func TestIndependentJobsRunConcurrently(t *testing.T) {
	ex := newScriptedExec()
	started := make(chan struct{}, 2)
	release := make(chan struct{})
	ex.on("A", func(_ *ExecContext, _ int) error {
		started <- struct{}{}
		<-release
		return nil
	})
	eng := New(ex)
	j1, _ := eng.Submit(&DagSpec{Nodes: []NodeSpec{{Name: "A", MaxAttempts: 1}}})
	j2, _ := eng.Submit(&DagSpec{Nodes: []NodeSpec{{Name: "A", MaxAttempts: 1}}})
	// Both jobs start node A and hold it open.
	<-started
	<-started
	// While both are held, they must execute concurrently (independent jobs).
	waitFor(t, func() bool {
		ex.mu.Lock()
		defer ex.mu.Unlock()
		return ex.active["A"] == 2
	}, time.Second)
	close(release)
	j1.Wait()
	j2.Wait()
	if j1.State() != JobSucceeded || j2.State() != JobSucceeded {
		t.Fatal("both jobs should succeed")
	}
}

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

func stateMap(j *Job) map[string]NodeState {
	out := map[string]NodeState{}
	for _, n := range j.Snapshot().Nodes {
		out[n.Name] = n.State
	}
	return out
}

func waitFor(t *testing.T, cond func() bool, within time.Duration) {
	t.Helper()
	deadline := time.Now().Add(within)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(time.Millisecond)
	}
	if !cond() {
		t.Fatalf("condition not met within %v", within)
	}
}
