package scheduler

import (
	"context"
	"fmt"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// ctxDepthKey carries recursion depth through task contexts in tests
// (SpawnFunc does not take a payload, so context is the vehicle).
type ctxDepthKey struct{}

func withDepth(ctx context.Context, d int) context.Context {
	return context.WithValue(ctx, ctxDepthKey{}, d)
}

func depthFrom(ctx context.Context) int {
	if v, ok := ctx.Value(ctxDepthKey{}).(int); ok {
		return v
	}
	return 0
}

func toIntI(v any) int {
	switch n := v.(type) {
	case int:
		return n
	case int64:
		return int(n)
	case float64:
		return int(n)
	default:
		return 0
	}
}

// recordingSink captures events and counts per-task executions when
// tests register task functions instrumented with it.
type recordingSink struct {
	mu     sync.Mutex
	events []Event
}

func newRecordingSink() *recordingSink { return &recordingSink{} }

func (s *recordingSink) Record(e Event) {
	s.mu.Lock()
	s.events = append(s.events, e)
	s.mu.Unlock()
}

func (s *recordingSink) slice() []Event {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]Event, len(s.events))
	copy(out, s.events)
	return out
}

func (s *recordingSink) count(typ EventType) int {
	s.mu.Lock()
	defer s.mu.Unlock()
	n := 0
	for _, e := range s.events {
		if e.Type == typ {
			n++
		}
	}
	return n
}

func newTestExecutor(t *testing.T, workers int, sink EventSink) *Executor {
	t.Helper()
	opts := []Option{}
	if sink != nil {
		opts = append(opts, WithSinks(sink))
	}
	if workers > 0 {
		opts = append(opts, WithWorkers(workers))
	}
	e, err := New(opts...)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if err := e.Shutdown(ctx); err != nil {
			t.Errorf("Shutdown: %v", err)
		}
	})
	return e
}

func waitTerminal(t *testing.T, e *Executor, h Handle, timeout time.Duration) TaskInfo {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	res, err := e.Result(ctx, h)
	if err != nil {
		t.Fatalf("Result: %v", err)
	}
	return res.TaskInfo
}

func TestSimpleSubmitSuccess(t *testing.T) {
	sink := newRecordingSink()
	e := newTestExecutor(t, 4, sink)
	h, err := e.SubmitFunc("test", func(ctx context.Context, rt Runtime) (any, error) {
		return 42, nil
	})
	if err != nil {
		t.Fatalf("Submit: %v", err)
	}
	info := waitTerminal(t, e, h, 2*time.Second)
	if info.State != StateSucceeded {
		t.Fatalf("state = %s, want succeeded", info.State)
	}
	if got := info.Value; got.(int) != 42 {
		t.Fatalf("value = %v, want 42", got)
	}
	if info.Attempts != 1 {
		t.Fatalf("attempts = %d, want 1", info.Attempts)
	}
	// Exactly one start event for the task.
	starts := 0
	for _, ev := range sink.slice() {
		if ev.Type == EventStarted && ev.TaskID == h.ID() {
			starts++
		}
	}
	if starts != 1 {
		t.Fatalf("started events = %d, want 1", starts)
	}
}

func TestTaskFailureAndPanic(t *testing.T) {
	e := newTestExecutor(t, 2, nil)

	hFail, _ := e.SubmitFunc("fail", func(ctx context.Context, rt Runtime) (any, error) {
		return nil, fmt.Errorf("boom")
	})
	info := waitTerminal(t, e, hFail, time.Second)
	if info.State != StateFailed || info.Err != "boom" {
		t.Fatalf("fail task info = %+v", info)
	}

	hPanic, _ := e.SubmitFunc("panic", func(ctx context.Context, rt Runtime) (any, error) {
		panic("kaboom")
	})
	info = waitTerminal(t, e, hPanic, time.Second)
	if info.State != StatePanicked {
		t.Fatalf("state = %s, want panicked", info.State)
	}
	if info.Err == "" {
		t.Fatal("panic error message should be recorded")
	}

	// A panicked task must not poison the executor: later work runs.
	hAfter, _ := e.SubmitFunc("after", func(ctx context.Context, rt Runtime) (any, error) {
		return "alive", nil
	})
	info = waitTerminal(t, e, hAfter, time.Second)
	if info.State != StateSucceeded {
		t.Fatalf("state after panic = %s", info.State)
	}
}

func TestSubmitAfterShutdownRejected(t *testing.T) {
	e := newTestExecutor(t, 1, nil)
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if err := e.Shutdown(ctx); err != nil {
		t.Fatalf("Shutdown: %v", err)
	}
	if _, err := e.SubmitFunc("late", func(ctx context.Context, rt Runtime) (any, error) {
		return nil, nil
	}); err != ErrExecutorShutdown {
		t.Fatalf("err = %v, want ErrExecutorShutdown", err)
	}
}

func TestInvalidWorkersRejected(t *testing.T) {
	if _, err := New(WithWorkers(0)); err == nil {
		t.Fatal("workers=0 should be rejected (use default by not setting the option)")
	}
}

// TestSingleWorkerDeepRecursionTree is the core starvation-deadlock
// canary: ONE worker executes a binary tree where every internal node
// spawns two children and blocks in Wait for both. If waiting did not
// help, the only worker would block forever at the root. The tree has
// depth D -> 2^(D+1)-1 nodes.
func TestSingleWorkerDeepRecursionTree(t *testing.T) {
	const depth = 9 // 2^10 - 1 = 1023 nodes, all on one worker
	sink := newRecordingSink()
	e := newTestExecutor(t, 1, sink)

	var starts atomic.Int64
	var recurse Func
	recurse = func(ctx context.Context, rt Runtime) (any, error) {
		starts.Add(1)
		if rt.WorkerID() != 1 {
			t.Errorf("ran on worker %d, want 1", rt.WorkerID())
		}
		d := depthFrom(ctx)
		if d == 0 {
			return 1, nil
		}
		h1, err := rt.SpawnFunc(withDepth(ctx, d-1), "recurse", recurse)
		if err != nil {
			return nil, err
		}
		h2, err := rt.SpawnFunc(withDepth(ctx, d-1), "recurse", recurse)
		if err != nil {
			return nil, err
		}
		v1, err := rt.Wait(ctx, h1)
		if err != nil {
			return nil, err
		}
		v2, err := rt.Wait(ctx, h2)
		if err != nil {
			return nil, err
		}
		return 1 + toIntI(v1) + toIntI(v2), nil
	}
	root, err := e.SubmitFunc("recurse", func(ctx context.Context, rt Runtime) (any, error) {
		return recurse(withDepth(ctx, depth), rt)
	})
	if err != nil {
		t.Fatalf("Submit: %v", err)
	}
	info := waitTerminal(t, e, root, 10*time.Second)
	if info.State != StateSucceeded {
		t.Fatalf("state = %s (%s), want succeeded", info.State, info.Err)
	}
	want := (1 << (depth + 1)) - 1
	if got := starts.Load(); int(got) != want {
		t.Fatalf("executions = %d, want %d", got, want)
	}
	if got := toIntI(info.Value); got != want {
		t.Fatalf("result = %d, want %d", got, want)
	}
	// Every task started exactly once.
	if n := sink.count(EventStarted); n != want {
		t.Fatalf("started events = %d, want %d", n, want)
	}
	if n := sink.count(EventCompleted); n != want {
		t.Fatalf("completed events = %d, want %d", n, want)
	}
}

func TestSingleWorkerLinearChain(t *testing.T) {
	e := newTestExecutor(t, 1, nil)
	const n = 200
	var chain Func
	chain = func(ctx context.Context, rt Runtime) (any, error) {
		remaining := depthFrom(ctx)
		if remaining == 0 {
			return 0, nil
		}
		h, err := rt.SpawnFunc(withDepth(ctx, remaining-1), "chain", chain)
		if err != nil {
			return nil, err
		}
		v, err := rt.Wait(ctx, h)
		if err != nil {
			return nil, err
		}
		return toIntI(v) + 1, nil
	}
	h, _ := e.SubmitFunc("chain", func(ctx context.Context, rt Runtime) (any, error) {
		return chain(withDepth(ctx, n), rt)
	})
	info := waitTerminal(t, e, h, 10*time.Second)
	if info.State != StateSucceeded {
		t.Fatalf("state = %s (%s)", info.State, info.Err)
	}
	if got := toIntI(info.Value); got != n {
		t.Fatalf("chain length = %d, want %d", got, n)
	}
}
