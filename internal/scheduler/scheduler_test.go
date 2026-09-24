package scheduler

import (
	"context"
	"errors"
	"fmt"
	"path/filepath"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"dagexec/internal/dag"
	"dagexec/internal/store"
)

func newTestScheduler(t *testing.T, opts Options) (*Scheduler, string) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "state.json")
	st, err := store.NewFileStore(path)
	if err != nil {
		t.Fatal(err)
	}
	if opts.RetryBaseDelay == 0 {
		opts.RetryBaseDelay = 5 * time.Millisecond
	}
	if opts.MaxRetryDelay == 0 {
		opts.MaxRetryDelay = time.Second
	}
	s := New(st, dag.DefaultRegistry(), opts)
	if err := s.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(s.Shutdown)
	return s, path
}

func waitTerminal(t *testing.T, s *Scheduler, id string, timeout time.Duration) *dag.DAGState {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	st, err := s.Wait(ctx, id, timeout)
	if err != nil {
		t.Fatalf("dag %s did not reach terminal status in %s: %v; status=%s", id, timeout, err, st.Status)
	}
	return st
}

func mustNode(t *testing.T, st *dag.DAGState, id string) *dag.NodeState {
	t.Helper()
	ns, ok := st.Nodes[id]
	if !ok {
		t.Fatalf("node %s missing", id)
	}
	return ns
}

// ---------------------------------------------------------------- acceptance

// Diamond: A -> B -> E, A -> C -> E. B fails permanently; E must become
// blocked; A and C stay successful. After a process restart (new scheduler
// over the same file), the confirmed-success nodes must not execute again.
func TestDiamondWithMiddleFailureAndRestart(t *testing.T) {
	s, path := newTestScheduler(t, Options{DefaultMaxAttempts: 2, DefaultMaxParallel: 4})

	spec := dag.Spec{Nodes: []dag.NodeSpec{
		{ID: "A", Task: "identity", Params: map[string]interface{}{"value": float64(7)}},
		{ID: "B", Task: "fail", Params: map[string]interface{}{"message": "boom"}, Deps: []string{"A"}, MaxAttempts: 2},
		{ID: "C", Task: "add", Deps: []string{"A"}},
		{ID: "E", Task: "collect", Deps: []string{"B", "C"}},
	}}
	st, err := s.Submit(spec)
	if err != nil {
		t.Fatal(err)
	}
	st = waitTerminal(t, s, st.ID, 5*time.Second)

	if st.Status != dag.DAGFailed {
		t.Fatalf("status=%s, want failed", st.Status)
	}
	if mustNode(t, st, "A").Status != dag.StatusSuccess {
		t.Fatal("A should succeed")
	}
	if mustNode(t, st, "C").Status != dag.StatusSuccess {
		t.Fatal("C should succeed independently of B")
	}
	if got := mustNode(t, st, "C").Result; got != float64(7) {
		t.Fatalf("C should reuse A's result via cache, got %v", got)
	}
	if mustNode(t, st, "B").Status != dag.StatusFailed || mustNode(t, st, "B").TotalRuns != 2 {
		t.Fatalf("B should fail after exactly 2 runs, got status=%s runs=%d",
			mustNode(t, st, "B").Status, mustNode(t, st, "B").TotalRuns)
	}
	if mustNode(t, st, "E").Status != dag.StatusBlocked {
		t.Fatalf("E should be blocked, got %s", mustNode(t, st, "E").Status)
	}
	runsBefore := map[string]int{}
	for id, ns := range st.Nodes {
		runsBefore[id] = ns.TotalRuns
	}

	// Simulate a real process restart: stop the scheduler, open a fresh
	// store and scheduler against the same state file.
	s.Shutdown()
	st2, err := store.NewFileStore(path)
	if err != nil {
		t.Fatal(err)
	}
	s2 := New(st2, dag.DefaultRegistry(), Options{RetryBaseDelay: 5 * time.Millisecond})
	if err := s2.Start(); err != nil {
		t.Fatal(err)
	}
	defer s2.Shutdown()
	after := waitTerminal(t, s2, st.ID, 3*time.Second)

	for id, ns := range after.Nodes {
		if ns.TotalRuns != runsBefore[id] {
			t.Fatalf("node %s executed after restart: runs before=%d after=%d",
				id, runsBefore[id], ns.TotalRuns)
		}
	}
	if mustNode(t, after, "A").Result != float64(7) {
		t.Fatalf("A cached result should survive restart, got %v", mustNode(t, after, "A").Result)
	}
	if after.Status != dag.DAGFailed || mustNode(t, after, "E").Status != dag.StatusBlocked {
		t.Fatalf("post-restart status dag=%s E=%s", after.Status, mustNode(t, after, "E").Status)
	}
}

// Retry reactivates a failed diamond. Successful nodes keep running; only
// the failed path re-executes.
func TestRetryAfterFixingFailure(t *testing.T) {
	s, _ := newTestScheduler(t, Options{DefaultMaxAttempts: 1})
	spec := dag.Spec{Nodes: []dag.NodeSpec{
		{ID: "A", Task: "flaky", Params: map[string]interface{}{
			"fail_times": float64(1), "succeed_with": float64(5)}},
		{ID: "B", Task: "add", Deps: []string{"A"}},
	}}
	st, err := s.Submit(spec)
	if err != nil {
		t.Fatal(err)
	}
	waitTerminal(t, s, st.ID, 3*time.Second)
	mid, _ := s.Get(st.ID)
	if mustNode(t, mid, "A").Status != dag.StatusFailed || mustNode(t, mid, "B").Status != dag.StatusBlocked {
		t.Fatalf("pre-retry: A=%s B=%s", mustNode(t, mid, "A").Status, mustNode(t, mid, "B").Status)
	}

	if err := s.Retry(st.ID); err != nil {
		t.Fatal(err)
	}
	fin := waitTerminal(t, s, st.ID, 3*time.Second)
	if fin.Status != dag.DAGSucceeded {
		t.Fatalf("status=%s, want succeeded", fin.Status)
	}
	if mustNode(t, fin, "A").TotalRuns != 2 {
		t.Fatalf("A should have 2 lifetime runs, got %d", mustNode(t, fin, "A").TotalRuns)
	}
	if mustNode(t, fin, "B").TotalRuns != 1 || mustNode(t, fin, "B").Result != float64(5) {
		t.Fatalf("B should run exactly once with A's cached value, got runs=%d result=%v",
			mustNode(t, fin, "B").TotalRuns, mustNode(t, fin, "B").Result)
	}
}

// Cancellation: while A is sleeping, cancel. No downstream node may ever
// leave "cancelled"/"pending" — in particular nothing new is launched.
func TestCancelStopsDownstream(t *testing.T) {
	s, _ := newTestScheduler(t, Options{DefaultMaxAttempts: 1})
	spec := dag.Spec{Nodes: []dag.NodeSpec{
		{ID: "A", Task: "sleep", Params: map[string]interface{}{"ms": float64(10000)}},
		{ID: "B", Task: "sleep", Params: map[string]interface{}{"ms": float64(10000)}, Deps: []string{"A"}},
		{ID: "C", Task: "identity", Params: map[string]interface{}{"value": "x"}, Deps: []string{"B"}},
	}}
	st, err := s.Submit(spec)
	if err != nil {
		t.Fatal(err)
	}
	time.Sleep(30 * time.Millisecond) // let A start
	if err := s.Cancel(st.ID); err != nil {
		t.Fatal(err)
	}
	fin := waitTerminal(t, s, st.ID, 3*time.Second)
	if fin.Status != dag.DAGCancelled {
		t.Fatalf("status=%s, want cancelled", fin.Status)
	}
	a := mustNode(t, fin, "A")
	if a.Status != dag.StatusCancelled || a.TotalRuns != 1 {
		t.Fatalf("A: status=%s runs=%d", a.Status, a.TotalRuns)
	}
	if b := mustNode(t, fin, "B"); b.Status != dag.StatusCancelled || b.TotalRuns != 0 {
		t.Fatalf("B must never launch: status=%s runs=%d", b.Status, b.TotalRuns)
	}
	if c := mustNode(t, fin, "C"); c.Status != dag.StatusCancelled || c.TotalRuns != 0 {
		t.Fatalf("C must never launch: status=%s runs=%d", c.Status, c.TotalRuns)
	}
}

// An independent branch must keep progressing while the failure fan-out is
// blocked.
func TestUnrelatedBranchKeepsRunning(t *testing.T) {
	s, _ := newTestScheduler(t, Options{DefaultMaxAttempts: 1})
	spec := dag.Spec{Nodes: []dag.NodeSpec{
		{ID: "bad", Task: "fail", Params: map[string]interface{}{"message": "x"}},
		{ID: "down", Task: "noop", Deps: []string{"bad"}},
		{ID: "solo", Task: "identity", Params: map[string]interface{}{"value": float64(1)}},
	}}
	st, _ := s.Submit(spec)
	fin := waitTerminal(t, s, st.ID, 3*time.Second)
	if fin.Status != dag.DAGFailed {
		t.Fatalf("status=%s", fin.Status)
	}
	if mustNode(t, fin, "solo").Status != dag.StatusSuccess {
		t.Fatal("independent node should succeed")
	}
	if mustNode(t, fin, "down").Status != dag.StatusBlocked {
		t.Fatal("downstream of failure should be blocked")
	}
}

// Finite retries with back-off: a flaky node that fails twice (3 attempts
// configured) eventually succeeds and its result is reused downstream.
func TestFiniteRetriesThenSuccess(t *testing.T) {
	s, _ := newTestScheduler(t, Options{DefaultMaxAttempts: 3, RetryBaseDelay: time.Millisecond})
	spec := dag.Spec{Nodes: []dag.NodeSpec{
		{ID: "A", Task: "flaky", Params: map[string]interface{}{
			"fail_times": float64(2), "succeed_with": float64(11)}},
		{ID: "B", Task: "add", Deps: []string{"A"}},
	}}
	st, _ := s.Submit(spec)
	fin := waitTerminal(t, s, st.ID, 3*time.Second)
	if fin.Status != dag.DAGSucceeded {
		t.Fatalf("status=%s", fin.Status)
	}
	a := mustNode(t, fin, "A")
	if a.TotalRuns != 3 || a.Attempt != 3 {
		t.Fatalf("A runs=%d attempt=%d, want 3/3", a.TotalRuns, a.Attempt)
	}
	if mustNode(t, fin, "B").Result != float64(11) {
		t.Fatalf("B result=%v", mustNode(t, fin, "B").Result)
	}
}

// Hard crash while a node is running: the node relaunches on restart
// (exactly once more), and other nodes are untouched.
func TestCrashWhileRunningRelaunchesNode(t *testing.T) {
	reg := dag.DefaultRegistry()
	var runs atomic.Int32
	reg["signal"] = func(ctx context.Context, in dag.Input) (interface{}, error) {
		n := runs.Add(1)
		if n == 1 {
			// Simulate the process being killed mid-execution: never return.
			<-ctx.Done()
			return nil, ctx.Err()
		}
		return fmt.Sprintf("ran-%d", n), nil
	}
	path := filepath.Join(t.TempDir(), "state.json")
	st, err := store.NewFileStore(path)
	if err != nil {
		t.Fatal(err)
	}
	s1 := New(st, reg, Options{DefaultMaxAttempts: 1, DefaultMaxParallel: 1})
	if err := s1.Start(); err != nil {
		t.Fatal(err)
	}
	d, err := s1.Submit(dag.Spec{Nodes: []dag.NodeSpec{
		{ID: "X", Task: "signal"},
	}})
	if err != nil {
		t.Fatal(err)
	}
	time.Sleep(30 * time.Millisecond)
	if runs.Load() != 1 {
		t.Fatalf("runs=%d, want 1 in-flight", runs.Load())
	}
	// Hard stop without waiting for the task: root context cancels it;
	// "running" remains on disk.
	s1.Shutdown()

	st2, err := store.NewFileStore(path)
	if err != nil {
		t.Fatal(err)
	}
	s2 := New(st2, reg, Options{DefaultMaxAttempts: 1})
	if err := s2.Start(); err != nil {
		t.Fatal(err)
	}
	defer s2.Shutdown()
	fin := waitTerminal(t, s2, d.ID, 3*time.Second)
	if fin.Status != dag.DAGSucceeded {
		t.Fatalf("status=%s", fin.Status)
	}
	if got := runs.Load(); got != 2 {
		t.Fatalf("node should run once before + once after crash, got %d", got)
	}
	if mustNode(t, fin, "X").Result != "ran-2" {
		t.Fatalf("result=%v", mustNode(t, fin, "X").Result)
	}
}

// Regression: a DAG that was cancelled (status=cancelled) but contains a
// node persisted as "running" (crash during cancellation of a hard kill)
// must stay cancelled after recovery instead of being resurrected.
func TestRecoverDoesNotResurrectCancelledDag(t *testing.T) {
	path := filepath.Join(t.TempDir(), "state.json")
	st, err := store.NewFileStore(path)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now().UTC()
	d := &dag.DAGState{
		ID: "crashed", Status: dag.DAGCancelled, CreatedAt: now, UpdatedAt: now,
		Spec: dag.Spec{MaxAttempts: 1, MaxParallel: 1, Nodes: []dag.NodeSpec{
			{ID: "A", Task: "sleep"},
			{ID: "B", Task: "noop", Deps: []string{"A"}},
		}},
		Nodes: map[string]*dag.NodeState{
			"A": {Status: dag.StatusRunning, Attempt: 1, TotalRuns: 1},
			"B": {Status: dag.StatusCancelled},
		},
	}
	if err := st.Create(d); err != nil {
		t.Fatal(err)
	}
	s := New(st, dag.DefaultRegistry(), Options{})
	if err := s.Start(); err != nil {
		t.Fatal(err)
	}
	defer s.Shutdown()
	time.Sleep(50 * time.Millisecond)
	got, err := s.Get("crashed")
	if err != nil {
		t.Fatal(err)
	}
	if got.Status != dag.DAGCancelled {
		t.Fatalf("status=%s, want cancelled (must not resurrect)", got.Status)
	}
	if got.Nodes["A"].Status != dag.StatusCancelled || got.Nodes["A"].TotalRuns != 1 {
		t.Fatalf("A: %+v", got.Nodes["A"])
	}
	if got.Nodes["B"].TotalRuns != 0 {
		t.Fatal("B must never run")
	}
}

func TestCancelUnknownDag(t *testing.T) {
	s, _ := newTestScheduler(t, Options{})
	if err := s.Cancel("deadbeef"); err != ErrNotFound {
		t.Fatalf("want ErrNotFound, got %v", err)
	}
	if err := s.Retry("deadbeef"); err != ErrNotFound {
		t.Fatalf("want ErrNotFound, got %v", err)
	}
}

func TestSubmitRejectsCycle(t *testing.T) {
	s, _ := newTestScheduler(t, Options{})
	_, err := s.Submit(dag.Spec{Nodes: []dag.NodeSpec{
		{ID: "a", Task: "noop", Deps: []string{"b"}},
		{ID: "b", Task: "noop", Deps: []string{"a"}},
	}})
	if err == nil {
		t.Fatal("expected validation error")
	}
}

func TestRetryRejectedWhileRunning(t *testing.T) {
	s, _ := newTestScheduler(t, Options{})
	st, _ := s.Submit(dag.Spec{Nodes: []dag.NodeSpec{{ID: "A", Task: "noop"}}})
	if err := s.Retry(st.ID); !errors.Is(err, ErrConflict) {
		t.Fatalf("want ErrConflict, got %v", err)
	}
	waitTerminal(t, s, st.ID, 2*time.Second)
	if err := s.Retry(st.ID); !errors.Is(err, ErrConflict) {
		t.Fatalf("retry on succeeded dag should conflict, got %v", err)
	}
}

// Concurrent Cancel and Retry on a failed DAG must not corrupt state or
// wedge the runner: the DAG ends in a consistent status and any subsequent
// valid Retry still drives it to success.
func TestConcurrentCancelAndRetry(t *testing.T) {
	s, _ := newTestScheduler(t, Options{DefaultMaxAttempts: 1})
	st, _ := s.Submit(dag.Spec{Nodes: []dag.NodeSpec{
		{ID: "A", Task: "flaky", Params: map[string]interface{}{
			"fail_times": float64(1), "succeed_with": "ok"}},
	}})
	waitTerminal(t, s, st.ID, 3*time.Second)

	for round := 0; round < 20; round++ {
		// Park the DAG at a terminal state each round.
		cur, _ := s.Get(st.ID)
		if cur.Status == dag.DAGRunning {
			waitTerminal(t, s, st.ID, 3*time.Second)
			cur, _ = s.Get(st.ID)
		}
		switch cur.Status {
		case dag.DAGFailed, dag.DAGCancelled:
		default:
			// Retry already won an earlier round and the flaky node
			// succeeded; nothing left to race.
			return
		}

		var wg sync.WaitGroup
		wg.Add(2)
		go func() { defer wg.Done(); _ = s.Cancel(st.ID) }()
		go func() { defer wg.Done(); _ = s.Retry(st.ID) }()
		wg.Wait()

		// The DAG must settle to a terminal status, never stay stuck in
		// "running" (a lost wake-up) or "cancelling".
		deadline := time.Now().Add(3 * time.Second)
		var mid *dag.DAGState
		for time.Now().Before(deadline) {
			mid, _ = s.Get(st.ID)
			if mid.Status != dag.DAGRunning && mid.Status != dag.DAGCancelling {
				break
			}
			time.Sleep(10 * time.Millisecond)
		}
		if mid.Status == dag.DAGRunning || mid.Status == dag.DAGCancelling {
			t.Fatalf("round %d: DAG stuck in %s", round, mid.Status)
		}
	}

	// Final clean retry: by now flaky is past its one failure or this is
	// the first reactivation; either way the DAG must succeed.
	cur, _ := s.Get(st.ID)
	if cur.Status == dag.DAGFailed || cur.Status == dag.DAGCancelled {
		if err := s.Retry(st.ID); err != nil {
			t.Fatalf("final retry: %v", err)
		}
		fin := waitTerminal(t, s, st.ID, 3*time.Second)
		if fin.Status != dag.DAGSucceeded {
			t.Fatalf("final retry should succeed, got %s", fin.Status)
		}
	}
}

func TestMaxParallelLimit(t *testing.T) {
	reg := dag.DefaultRegistry()
	var active atomic.Int32
	var maxSeen atomic.Int32
	reg["probe"] = func(ctx context.Context, in dag.Input) (interface{}, error) {
		cur := active.Add(1)
		for {
			old := maxSeen.Load()
			if cur <= old || maxSeen.CompareAndSwap(old, cur) {
				break
			}
		}
		time.Sleep(20 * time.Millisecond)
		active.Add(-1)
		return nil, nil
	}
	path := filepath.Join(t.TempDir(), "state.json")
	st, _ := store.NewFileStore(path)
	s := New(st, reg, Options{DefaultMaxAttempts: 1, DefaultMaxParallel: 2})
	_ = s.Start()
	defer s.Shutdown()

	var nodes []dag.NodeSpec
	for i := 0; i < 6; i++ {
		nodes = append(nodes, dag.NodeSpec{ID: fmt.Sprintf("n%d", i), Task: "probe"})
	}
	d, err := s.Submit(dag.Spec{Nodes: nodes})
	if err != nil {
		t.Fatal(err)
	}
	waitTerminal(t, s, d.ID, 5*time.Second)
	if max := maxSeen.Load(); max > 2 {
		t.Fatalf("observed %d concurrent tasks, limit is 2", max)
	}
}
