package engine_test

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync"
	"testing"
	"time"

	"dagexec/internal/engine"
	"dagexec/internal/model"
	"dagexec/internal/store"
	"dagexec/internal/task"
)

// ---------------------------------------------------------------------------
// Test helpers
// ---------------------------------------------------------------------------

// gateTask blocks until release is closed or ctx is cancelled, then records
// (once) that it started and returns err. Used to assert whether a node was
// ever launched.
type gateTask struct {
	mu      sync.Mutex
	started int
	release chan struct{}
	err     error
	result  any
}

func newGate(err error, result any) *gateTask {
	return &gateTask{release: make(chan struct{}), err: err, result: result}
}

func (g *gateTask) fn(ctx context.Context, _ map[string]any, _ map[string]any) (any, error) {
	g.mu.Lock()
	g.started++
	g.mu.Unlock()
	select {
	case <-g.release:
		return g.result, g.err
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}

func (g *gateTask) runs() int {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.started
}

func (g *gateTask) releaseNow() { close(g.release) }

// failNTimes succeeds on attempt > fails, counting launches.
type failNTimes struct {
	mu    sync.Mutex
	calls int
	fails int
}

func (f *failNTimes) fn(_ context.Context, _ map[string]any, _ map[string]any) (any, error) {
	f.mu.Lock()
	f.calls++
	n := f.calls
	f.mu.Unlock()
	if n <= f.fails {
		return nil, fmt.Errorf("attempt %d failed", n)
	}
	return "ok-after-retry", nil
}

func (f *failNTimes) callsCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.calls
}

func newEngine(t *testing.T, dir string, reg *task.Registry) (*engine.Engine, context.CancelFunc) {
	t.Helper()
	st, err := store.NewFileStore(dir)
	if err != nil {
		t.Fatalf("store: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	eng := engine.New(st, reg)
	if err := eng.Start(ctx); err != nil {
		t.Fatalf("start: %v", err)
	}
	return eng, cancel
}

func waitFor(t *testing.T, eng *engine.Engine, id string, timeout time.Duration) *model.Snapshot {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		snap, err := eng.Get(id)
		if err != nil {
			t.Fatalf("get: %v", err)
		}
		if snap.Status.Terminal() {
			return snap
		}
		time.Sleep(5 * time.Millisecond)
	}
	snap, _ := eng.Get(id)
	t.Fatalf("dag %s did not finish in %v; status=%s", id, timeout, snap.Status)
	return nil
}

func intp(n int) *int { return &n }

func toFloat(t *testing.T, v any) float64 {
	t.Helper()
	switch n := v.(type) {
	case float64:
		return n
	case int:
		return float64(n)
	case int64:
		return float64(n)
	default:
		t.Fatalf("not a number: %T(%v)", v, v)
		return 0
	}
}

// ---------------------------------------------------------------------------
// Validation: cycles and missing deps
// ---------------------------------------------------------------------------

func TestSubmitRejectsCycle(t *testing.T) {
	dir := t.TempDir()
	eng, cancel := newEngine(t, dir, task.Builtins())
	defer cancel()

	_, err := eng.Submit(model.DAG{Nodes: []model.Node{
		{ID: "a", Type: "const", Deps: []string{"c"}, Params: map[string]any{"value": 1}},
		{ID: "b", Type: "const", Deps: []string{"a"}, Params: map[string]any{"value": 1}},
		{ID: "c", Type: "const", Deps: []string{"b"}, Params: map[string]any{"value": 1}},
	}})
	if err == nil {
		t.Fatal("expected cycle error, got nil")
	}
	if !strings.Contains(err.Error(), "cycle") {
		t.Fatalf("error should mention cycle, got: %v", err)
	}
}

func TestSubmitRejectsSelfCycle(t *testing.T) {
	dir := t.TempDir()
	eng, cancel := newEngine(t, dir, task.Builtins())
	defer cancel()
	_, err := eng.Submit(model.DAG{Nodes: []model.Node{
		{ID: "a", Type: "const", Deps: []string{"a"}, Params: map[string]any{"value": 1}},
	}})
	if err == nil || !strings.Contains(err.Error(), "itself") {
		t.Fatalf("expected self-dependency error, got %v", err)
	}
}

func TestSubmitRejectsMissingDependency(t *testing.T) {
	dir := t.TempDir()
	eng, cancel := newEngine(t, dir, task.Builtins())
	defer cancel()
	_, err := eng.Submit(model.DAG{Nodes: []model.Node{
		{ID: "a", Type: "const", Deps: []string{"ghost"}, Params: map[string]any{"value": 1}},
	}})
	if err == nil || !strings.Contains(err.Error(), "missing dependency") {
		t.Fatalf("expected missing dependency error, got %v", err)
	}
}

func TestSubmitRejectsUnknownType(t *testing.T) {
	dir := t.TempDir()
	eng, cancel := newEngine(t, dir, task.Builtins())
	defer cancel()
	_, err := eng.Submit(model.DAG{Nodes: []model.Node{
		{ID: "a", Type: "os/exec"},
	}})
	if err == nil || !strings.Contains(err.Error(), "unknown task type") {
		t.Fatalf("expected unknown type error, got %v", err)
	}
}

func TestSubmitRejectsDuplicateID(t *testing.T) {
	dir := t.TempDir()
	eng, cancel := newEngine(t, dir, task.Builtins())
	defer cancel()
	_, err := eng.Submit(model.DAG{Nodes: []model.Node{
		{ID: "a", Type: "const", Params: map[string]any{"value": 1}},
		{ID: "a", Type: "const", Params: map[string]any{"value": 2}},
	}})
	if err == nil || !strings.Contains(err.Error(), "duplicate") {
		t.Fatalf("expected duplicate error, got %v", err)
	}
}

// ---------------------------------------------------------------------------
// Diamond DAG: happy path, $ref resolution, result cache
// ---------------------------------------------------------------------------

//	top (10)
//	/     \
//
// left(*2) right(+5)
//
//	 \     /
//	bottom: sum_deps(left,right) = 20 + 15 = 35
func TestDiamondSucceedsWithRefs(t *testing.T) {
	dir := t.TempDir()
	eng, cancel := newEngine(t, dir, task.Builtins())
	defer cancel()

	snap, err := eng.Submit(model.DAG{Nodes: []model.Node{
		{ID: "top", Type: "const", Params: map[string]any{"value": 10}},
		{ID: "left", Type: "mul", Deps: []string{"top"},
			Params: map[string]any{"x": map[string]any{"$ref": "top"}, "y": 2}},
		{ID: "right", Type: "add", Deps: []string{"top"},
			Params: map[string]any{"x": map[string]any{"$ref": "top"}, "y": 5}},
		{ID: "bottom", Type: "sum_deps", Deps: []string{"left", "right"}},
	}})
	if err != nil {
		t.Fatalf("submit: %v", err)
	}
	snap = waitFor(t, eng, snap.ID, 2*time.Second)

	if snap.Status != model.StatusSucceeded {
		t.Fatalf("status=%s nodes=%+v", snap.Status, snap.Nodes)
	}
	for _, id := range []string{"top", "left", "right", "bottom"} {
		if snap.Nodes[id].Status != model.StatusSucceeded {
			t.Errorf("node %s = %s, want succeeded", id, snap.Nodes[id].Status)
		}
		if snap.Nodes[id].Attempts != 1 {
			t.Errorf("node %s attempts=%d, want 1", id, snap.Nodes[id].Attempts)
		}
	}
	if got := toFloat(t, snap.Nodes["top"].Result); got != 10 {
		t.Errorf("top result=%v want 10", snap.Nodes["top"].Result)
	}
	if got := toFloat(t, snap.Nodes["left"].Result); got != 20 {
		t.Errorf("left result=%v want 20", snap.Nodes["left"].Result)
	}
	if got := toFloat(t, snap.Nodes["right"].Result); got != 15 {
		t.Errorf("right result=%v want 15", snap.Nodes["right"].Result)
	}
	if got := toFloat(t, snap.Nodes["bottom"].Result); got != 35 {
		t.Errorf("bottom result=%v want 35", snap.Nodes["bottom"].Result)
	}
}

// ---------------------------------------------------------------------------
// Middle task fails: failure + finite retries, downstream skipped
// ---------------------------------------------------------------------------

func TestMiddleFailureSkipsDownstream(t *testing.T) {
	dir := t.TempDir()
	flaky := &failNTimes{fails: 2} // succeeds on 3rd attempt
	alwaysFail := newGate(errors.New("boom"), nil)
	alwaysFail.releaseNow()
	reg := task.NewRegistry(map[string]task.Func{
		"flaky":      flaky.fn,
		"alwaysfail": alwaysFail.fn,
		"const":      func(_ context.Context, p map[string]any, _ map[string]any) (any, error) { return p["value"], nil },
	})
	eng, cancel := newEngine(t, dir, reg)
	defer cancel()

	// top -> broken(always fails, 1 retry = 2 attempts) -> leaf
	snap, err := eng.Submit(model.DAG{
		BackoffMs: 1,
		Nodes: []model.Node{
			{ID: "top", Type: "const", Params: map[string]any{"value": 1}},
			{ID: "broken", Type: "alwaysfail", Deps: []string{"top"}, Retries: intp(1)},
			{ID: "leaf", Type: "const", Deps: []string{"broken"}, Params: map[string]any{"value": 2}},
		},
	})
	if err != nil {
		t.Fatalf("submit: %v", err)
	}
	snap = waitFor(t, eng, snap.ID, 2*time.Second)

	if snap.Status != model.StatusFailed {
		t.Fatalf("status=%s want failed", snap.Status)
	}
	if snap.Nodes["top"].Status != model.StatusSucceeded {
		t.Errorf("top=%s want succeeded", snap.Nodes["top"].Status)
	}
	if snap.Nodes["broken"].Status != model.StatusFailed {
		t.Errorf("broken=%s want failed (%s)", snap.Nodes["broken"].Status, snap.Nodes["broken"].Error)
	}
	if got := snap.Nodes["broken"].Attempts; got != 2 {
		t.Errorf("broken attempts=%d want 2 (1 try + 1 retry)", got)
	}
	if snap.Nodes["leaf"].Status != model.StatusSkipped {
		t.Errorf("leaf=%s want skipped", snap.Nodes["leaf"].Status)
	}
	if snap.Nodes["leaf"].Attempts != 0 {
		t.Errorf("leaf attempts=%d, must never execute", snap.Nodes["leaf"].Attempts)
	}

	// A node configured to fail N times then succeed recovers via retries.
	flakySnap, err := eng.Submit(model.DAG{
		BackoffMs: 1,
		Nodes: []model.Node{
			{ID: "f", Type: "flaky", Retries: intp(5)},
		},
	})
	if err != nil {
		t.Fatalf("submit: %v", err)
	}
	flakySnap = waitFor(t, eng, flakySnap.ID, 2*time.Second)
	if flakySnap.Status != model.StatusSucceeded {
		t.Fatalf("flaky dag=%s, want succeeded; err=%s", flakySnap.Status, flakySnap.Nodes["f"].Error)
	}
	if flaky.callsCount() != 3 {
		t.Errorf("flaky calls=%d want 3", flaky.callsCount())
	}
}

// ---------------------------------------------------------------------------
// ACCEPTANCE: restart does not re-execute confirmed-succeeded nodes
// ---------------------------------------------------------------------------

// Diamond where the middle node is gated; we kill the process while middle is
// running. top must already be succeeded; after restart top must NOT run
// again, middle is retried from pending, and the diamond completes.
func TestRestartSkipsSucceededAndRetriesInflight(t *testing.T) {
	dir := t.TempDir()

	// Registry state must survive "process restart" — emulate with fresh
	// engine instances over the same dir. Counters track launches across
	// instances.
	var topMu sync.Mutex
	topRuns := 0
	var midMu sync.Mutex
	midRuns := 0
	midGate := make(chan struct{})
	var midGateOnce sync.Once

	mkReg := func() *task.Registry {
		return task.NewRegistry(map[string]task.Func{
			"top": func(context.Context, map[string]any, map[string]any) (any, error) {
				topMu.Lock()
				topRuns++
				topMu.Unlock()
				return int64(10), nil
			},
			"mid": func(ctx context.Context, _ map[string]any, _ map[string]any) (any, error) {
				midMu.Lock()
				midRuns++
				midMu.Unlock()
				select {
				case <-midGate:
					return int64(42), nil
				case <-ctx.Done():
					return nil, ctx.Err()
				}
			},
			"leaf": func(_ context.Context, _ map[string]any, deps map[string]any) (any, error) {
				v := deps["mid"].(int64)
				return v + 1, nil
			},
		})
	}

	// --- instance 1 ---
	eng1, cancel1 := newEngine(t, dir, mkReg())
	snap, err := eng1.Submit(model.DAG{Nodes: []model.Node{
		{ID: "top", Type: "top"},
		{ID: "mid", Type: "mid", Deps: []string{"top"}},
		{ID: "leaf", Type: "leaf", Deps: []string{"mid"}},
	}})
	if err != nil {
		t.Fatalf("submit: %v", err)
	}
	id := snap.ID

	// Wait until mid is running (top durably succeeded).
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		s, _ := eng1.Get(id)
		if s.Nodes["mid"].Status == model.StatusRunning {
			break
		}
		time.Sleep(2 * time.Millisecond)
	}
	if s, _ := eng1.Get(id); s.Nodes["mid"].Status != model.StatusRunning {
		t.Fatalf("mid never reached running: %s", s.Nodes["mid"].Status)
	}

	// "Crash": cancel the engine context (like SIGTERM mid-run) and shut down
	// without releasing the gate. Shutdown rolls mid back to pending on disk.
	shCtx, shCancel := context.WithTimeout(context.Background(), 2*time.Second)
	if err := eng1.Shutdown(shCtx); err != nil {
		t.Fatalf("shutdown: %v", err)
	}
	shCancel()
	cancel1()
	midGateOnce.Do(func() {}) // no-op; gate stays closed across restart
	if topRuns != 1 {
		t.Fatalf("precondition: top ran %d times, want 1", topRuns)
	}

	// --- instance 2: fresh engine over the same data dir ---
	eng2, cancel2 := newEngine(t, dir, mkReg())
	defer cancel2()

	// Let mid proceed now.
	close(midGate)

	snap2 := waitFor(t, eng2, id, 2*time.Second)
	if snap2.Status != model.StatusSucceeded {
		t.Fatalf("after restart status=%s (%s)", snap2.Status, snap2.Nodes["mid"].Error)
	}
	if topRuns != 1 {
		t.Errorf("top executed %d times across restart, want exactly 1 (confirmed success reused)", topRuns)
	}
	if midRuns != 2 {
		t.Errorf("mid executed %d times total, want 2 (in-flight attempt rolled back + 1 retry)", midRuns)
	}
	if got := toFloat(t, snap2.Nodes["top"].Result); got != 10 {
		t.Errorf("top cached result=%v want 10", snap2.Nodes["top"].Result)
	}
	if got := toFloat(t, snap2.Nodes["mid"].Result); got != 42 {
		t.Errorf("mid result=%v want 42", snap2.Nodes["mid"].Result)
	}
	if got := toFloat(t, snap2.Nodes["leaf"].Result); got != 43 {
		t.Errorf("leaf result=%v want 43", snap2.Nodes["leaf"].Result)
	}
}

// ---------------------------------------------------------------------------
// ACCEPTANCE: cancel must not start new downstream nodes
// ---------------------------------------------------------------------------

func TestCancelStopsDownstream(t *testing.T) {
	dir := t.TempDir()
	rootGate := newGate(nil, int64(1))
	leafGate := newGate(nil, "leaf-done")
	called := &sync.Map{}
	reg := task.NewRegistry(map[string]task.Func{
		"root": rootGate.fn,
		"mid": func(_ context.Context, p map[string]any, _ map[string]any) (any, error) {
			called.Store("mid", true)
			return p["v"], nil
		},
		"leaf": func(ctx context.Context, p map[string]any, _ map[string]any) (any, error) {
			called.Store("leaf", true)
			return leafGate.fn(ctx, p, nil)
		},
	})
	eng, cancel := newEngine(t, dir, reg)
	defer cancel()

	// root (gated) -> mid -> leaf ; also a second branch root -> leaf2
	snap, err := eng.Submit(model.DAG{Nodes: []model.Node{
		{ID: "root", Type: "root"},
		{ID: "mid", Type: "mid", Deps: []string{"root"}, Params: map[string]any{"v": 1}},
		{ID: "leaf", Type: "leaf", Deps: []string{"mid"}, Params: map[string]any{}},
		{ID: "leaf2", Type: "mid", Deps: []string{"root"}, Params: map[string]any{"v": 2}},
	}})
	if err != nil {
		t.Fatalf("submit: %v", err)
	}
	// Wait for root to be in flight.
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		s, _ := eng.Get(snap.ID)
		if s.Nodes["root"].Status == model.StatusRunning {
			break
		}
		time.Sleep(2 * time.Millisecond)
	}

	cancelled, err := eng.Cancel(snap.ID)
	if err != nil {
		t.Fatalf("cancel: %v", err)
	}
	if cancelled.Status != model.StatusCancelled {
		t.Fatalf("status=%s want cancelled", cancelled.Status)
	}

	// Release root so the in-flight task finishes; give the scheduler time
	// it must NOT use.
	rootGate.releaseNow()
	time.Sleep(100 * time.Millisecond)

	final, err := eng.Get(snap.ID)
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	if final.Status != model.StatusCancelled {
		t.Fatalf("status=%s want cancelled", final.Status)
	}
	for _, id := range []string{"mid", "leaf", "leaf2"} {
		if final.Nodes[id].Status != model.StatusCancelled {
			t.Errorf("node %s=%s want cancelled", id, final.Nodes[id].Status)
		}
		if final.Nodes[id].Attempts != 0 {
			t.Errorf("node %s attempts=%d, must never run", id, final.Nodes[id].Attempts)
		}
	}
	if _, ran := called.Load("mid"); ran {
		t.Error("mid executed after cancel")
	}
	if _, ran := called.Load("leaf"); ran {
		t.Error("leaf executed after cancel")
	}

	// Cancelling again is idempotent and returns the same terminal snapshot.
	again, err := eng.Cancel(snap.ID)
	if err != nil {
		t.Fatalf("second cancel: %v", err)
	}
	if again.Status != model.StatusCancelled {
		t.Errorf("re-cancel status=%s", again.Status)
	}
}

// Cancel after some nodes succeeded: successes stay cached, nothing new runs.
func TestCancelKeepsSucceeded(t *testing.T) {
	dir := t.TempDir()
	gate := newGate(nil, "go")
	ran := &sync.Map{}
	reg := task.NewRegistry(map[string]task.Func{
		"first": func(context.Context, map[string]any, map[string]any) (any, error) {
			return int64(7), nil
		},
		"gated": gate.fn,
		"never": func(context.Context, map[string]any, map[string]any) (any, error) {
			ran.Store("never", true)
			return nil, errors.New("should not run")
		},
	})
	eng, cancel := newEngine(t, dir, reg)
	defer cancel()

	snap, err := eng.Submit(model.DAG{Nodes: []model.Node{
		{ID: "first", Type: "first"},
		{ID: "gated", Type: "gated", Deps: []string{"first"}},
		{ID: "never", Type: "never", Deps: []string{"gated"}},
	}})
	if err != nil {
		t.Fatal(err)
	}
	// gated running means first is durably done.
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		s, _ := eng.Get(snap.ID)
		if s.Nodes["gated"].Status == model.StatusRunning {
			break
		}
		time.Sleep(2 * time.Millisecond)
	}
	if _, err := eng.Cancel(snap.ID); err != nil {
		t.Fatal(err)
	}
	gate.releaseNow()
	time.Sleep(50 * time.Millisecond)

	final, _ := eng.Get(snap.ID)
	if final.Nodes["first"].Status != model.StatusSucceeded || toFloat(t, final.Nodes["first"].Result) != 7 {
		t.Errorf("first = %s/%v, want succeeded/7", final.Nodes["first"].Status, final.Nodes["first"].Result)
	}
	// gated was in flight at cancel time. Interrupted tasks end cancelled;
	// one that completes before observing the signal keeps its success.
	// Either way it must not be retried/failed and must not schedule work.
	switch final.Nodes["gated"].Status {
	case model.StatusCancelled, model.StatusSucceeded:
	default:
		t.Errorf("gated=%s want cancelled or succeeded", final.Nodes["gated"].Status)
	}
	if final.Nodes["gated"].Attempts != 1 {
		t.Errorf("gated attempts=%d want 1 (no retries after cancel)", final.Nodes["gated"].Attempts)
	}
	if final.Nodes["never"].Status != model.StatusCancelled {
		t.Errorf("never=%s want cancelled", final.Nodes["never"].Status)
	}
	if _, ok := ran.Load("never"); ok {
		t.Error("never node executed")
	}
}

// ---------------------------------------------------------------------------
// Restart after a completed DAG: nothing re-runs at all.
// ---------------------------------------------------------------------------

func TestRestartTerminalDAGDoesNotReplay(t *testing.T) {
	dir := t.TempDir()
	var mu sync.Mutex
	runs := map[string]int{}
	mkReg := func() *task.Registry {
		return task.NewRegistry(map[string]task.Func{
			"inc": func(_ context.Context, p map[string]any, _ map[string]any) (any, error) {
				mu.Lock()
				runs[p["id"].(string)]++
				mu.Unlock()
				return "done", nil
			},
		})
	}
	eng1, _ := newEngine(t, dir, mkReg())
	snap, err := eng1.Submit(model.DAG{Nodes: []model.Node{
		{ID: "a", Type: "inc", Params: map[string]any{"id": "a"}},
		{ID: "b", Type: "inc", Deps: []string{"a"}, Params: map[string]any{"id": "b"}},
	}})
	if err != nil {
		t.Fatal(err)
	}
	waitFor(t, eng1, snap.ID, 2*time.Second)
	if err := eng1.Shutdown(context.Background()); err != nil {
		t.Fatalf("shutdown eng1: %v", err)
	}

	eng2, cancel2 := newEngine(t, dir, mkReg())
	defer cancel2()
	s2, err := eng2.Get(snap.ID)
	if err != nil {
		t.Fatal(err)
	}
	if s2.Status != model.StatusSucceeded {
		t.Fatalf("status=%s", s2.Status)
	}
	time.Sleep(50 * time.Millisecond)
	mu.Lock()
	defer mu.Unlock()
	if runs["a"] != 1 || runs["b"] != 1 {
		t.Fatalf("runs after restart=%v, want each exactly 1", runs)
	}
}
