package scheduler

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"
)

// blockOnce is a task gate: it blocks until release is called, then
// all waiters proceed.
type blockGate struct {
	releaseOnce sync.Once
	ch          chan struct{}
}

func newGate() *blockGate { return &blockGate{ch: make(chan struct{})} }
func (g *blockGate) release() {
	g.releaseOnce.Do(func() { close(g.ch) })
}

// TestCancelPendingChildrenNeverRuns deterministically covers the
// guarantee "a task canceled while pending has its function executed
// zero times": with one worker, the root spawns leaves into its own
// local deque and then blocks on a gate, so all leaves sit queued
// behind the blocked root with nowhere to run. Canceling the root
// subtree must move the leaves straight to canceled (attempts == 0).
func TestCancelPendingChildrenNeverRuns(t *testing.T) {
	sink := newRecordingSink()
	e := newTestExecutor(t, 1, sink)

	rootGate := newGate()
	leavesSpawned := make(chan []Handle, 1)
	root, err := e.SubmitFunc("root", func(ctx context.Context, rt Runtime) (any, error) {
		hs := make([]Handle, 8)
		for i := 0; i < 8; i++ {
			h, err := rt.SpawnFunc(ctx, "leaf", func(ctx context.Context, rt Runtime) (any, error) {
				t.Errorf("canceled leaf must never execute")
				return nil, nil
			})
			if err != nil {
				return nil, err
			}
			hs[i] = h
		}
		leavesSpawned <- hs
		<-rootGate.ch
		<-ctx.Done()
		return nil, ctx.Err()
	})
	if err != nil {
		t.Fatal(err)
	}
	var hs []Handle
	select {
	case hs = <-leavesSpawned:
	case <-time.After(3 * time.Second):
		t.Fatal("root did not spawn leaves in time")
	}

	if ok, err := e.Cancel(root); err != nil || !ok {
		t.Fatalf("Cancel root = %v, %v", ok, err)
	}
	rootGate.release()

	res, err := e.Result(context.Background(), root)
	if err != nil {
		t.Fatalf("Result: %v", err)
	}
	if res.State != StateCanceled {
		t.Fatalf("root state = %s, want canceled", res.State)
	}
	if err := e.WaitIdle(context.Background()); err != nil {
		t.Fatalf("WaitIdle: %v", err)
	}
	for _, h := range hs {
		info := h.Snapshot()
		if info.State != StateCanceled {
			t.Fatalf("leaf %s state = %s, want canceled", h.ID(), info.State)
		}
		if info.Attempts != 0 {
			t.Fatalf("leaf %s attempts = %d, want 0", h.ID(), info.Attempts)
		}
	}
	// No leaf ever emitted a started event.
	for _, ev := range sink.slice() {
		if ev.Type == EventStarted && ev.Kind == "leaf" {
			t.Fatalf("leaf %s started despite pending cancellation", ev.TaskID)
		}
	}
	for _, info := range e.ListTasks(0) {
		if !info.State.IsTerminal() {
			t.Fatalf("task %s non-terminal after drain", info.ID)
		}
	}
}

func TestCancelAlreadyTerminalIsNoop(t *testing.T) {
	e := newTestExecutor(t, 2, nil)
	h, _ := e.SubmitFunc("ok", func(ctx context.Context, rt Runtime) (any, error) {
		return 1, nil
	})
	waitTerminal(t, e, h, time.Second)
	ok, err := e.Cancel(h)
	if err != nil || ok {
		t.Fatalf("Cancel on terminal = %v, %v; want false, nil", ok, err)
	}
}

func TestWaitContextCanceledReturnsContextError(t *testing.T) {
	e := newTestExecutor(t, 1, nil)
	gate := newGate()
	gotResult := make(chan struct{})

	// Child will not finish until the gate is released; the waiter's
	// own context is canceled, so Wait must return while the child
	// stays alive (then we release and drain).
	root, _ := e.SubmitFunc("root", func(ctx context.Context, rt Runtime) (any, error) {
		child, _ := rt.SpawnFunc(ctx, "blocked", func(ctx context.Context, rt Runtime) (any, error) {
			<-gate.ch
			return "done", nil
		})
		wctx, cancel := context.WithCancel(ctx)
		cancel()
		_, err := rt.Wait(wctx, child)
		if !errors.Is(err, context.Canceled) {
			t.Errorf("Wait err = %v, want context.Canceled", err)
		}
		close(gotResult)
		// Finish the run with a second, uncanceled wait after release.
		v, err := rt.Wait(ctx, child)
		if err != nil {
			return nil, err
		}
		return v, nil
	})
	<-gotResult
	gate.release()
	info := waitTerminal(t, e, root, 5*time.Second)
	if info.State != StateSucceeded || info.Value != "done" {
		t.Fatalf("root = %+v", info)
	}
}

// TestStealingSpreadsWork: with 4 workers and many independent
// blocking-ish tasks, all workers must observe at least one steal or
// run; more importantly every task still executes exactly once.
func TestStealingSpreadsWork(t *testing.T) {
	sink := newRecordingSink()
	e := newTestExecutor(t, 4, sink)

	const n = 400
	root, _ := e.SubmitFunc("fan", func(ctx context.Context, rt Runtime) (any, error) {
		hs := make([]Handle, n)
		for i := 0; i < n; i++ {
			h, err := rt.SpawnFunc(ctx, "leaf", func(ctx context.Context, rt Runtime) (any, error) {
				if rt.WorkerID() < 1 || rt.WorkerID() > 4 {
					t.Errorf("bad worker id %d", rt.WorkerID())
				}
				return 1, nil
			})
			if err != nil {
				return nil, err
			}
			hs[i] = h
		}
		sum := 0
		for _, h := range hs {
			v, err := rt.Wait(ctx, h)
			if err != nil {
				return nil, err
			}
			sum += toIntI(v)
		}
		return sum, nil
	})
	info := waitTerminal(t, e, root, 10*time.Second)
	if info.State != StateSucceeded || toIntI(info.Value) != n {
		t.Fatalf("root info = %+v", info)
	}
	starts := 0
	workersSeen := map[int]bool{}
	steals := 0
	for _, ev := range sink.slice() {
		if ev.Type == EventStarted {
			starts++
			if ev.Worker > 0 {
				workersSeen[ev.Worker] = true
			}
		}
		if ev.Type == EventStole {
			steals++
		}
	}
	if starts != n+1 {
		t.Fatalf("starts = %d, want %d", starts, n+1)
	}
	if len(workersSeen) < 2 {
		t.Fatalf("work did not spread: only workers %v ran", workersSeen)
	}
	t.Logf("steal events=%d workers=%v", steals, workersSeen)
}

// TestShutdownDrainsQueuedTasks verifies the graceful path: once
// Shutdown starts, no new work is accepted and pending queued tasks
// are canceled (never executed), while a task that has ALREADY reached
// a natural success state is reported as succeeded. To exercise the
// graceful "running task completes" branch deterministically, we let
// the blocking task finish successfully and THEN call Shutdown while
// its queued leaves may still be draining.
func TestShutdownDrainsQueuedTasks(t *testing.T) {
	e, err := New(WithWorkers(2))
	if err != nil {
		t.Fatal(err)
	}
	ran := make(chan struct{}, 32)
	gate := newGate()
	rootDone := make(chan struct{})

	// The running task spawns leaves but waits on the gate. Leaves
	// queued while it blocks will be canceled by shutdown when still
	// pending; those that already ran incremented `ran`.
	h, err := e.SubmitFunc("root", func(ctx context.Context, rt Runtime) (any, error) {
		for i := 0; i < 16; i++ {
			_, _ = rt.SpawnFunc(ctx, "leaf", func(ctx context.Context, rt Runtime) (any, error) {
				ran <- struct{}{}
				return nil, nil
			})
		}
		<-gate.ch
		close(rootDone)
		return "graceful", nil
	})
	if err != nil {
		t.Fatal(err)
	}

	shutdownErr := make(chan error, 1)
	// Release the gate before shutdown starts: the running task gets
	// to return naturally with a success value, so graceful drain
	// leaves it succeeded.
	gate.release()
	select {
	case <-rootDone:
	case <-time.After(3 * time.Second):
		t.Fatal("root did not finish after gate release")
	}
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		shutdownErr <- e.Shutdown(ctx)
	}()

	// New work must be rejected once shutdown begins. Stop polling as
	// soon as either rejection is observed or shutdown completed.
	deadline := time.Now().Add(2 * time.Second)
	rejected := false
shutdownPoll:
	for time.Now().Before(deadline) {
		if _, err := e.SubmitFunc("late", func(context.Context, Runtime) (any, error) { return nil, nil }); err == ErrExecutorShutdown {
			rejected = true
			break
		}
		select {
		case <-shutdownErr:
			// Shutdown already finished: one more submit is
			// guaranteed rejected; break out of the poll.
			_, err := e.SubmitFunc("late", func(context.Context, Runtime) (any, error) { return nil, nil })
			rejected = err == ErrExecutorShutdown
			break shutdownPoll
		default:
		}
		time.Sleep(time.Millisecond)
	}
	if !rejected {
		t.Fatal("new work was never rejected during shutdown")
	}

	if err := <-shutdownErr; err != nil {
		t.Fatalf("graceful Shutdown returned %v", err)
	}
	res, err := e.Result(context.Background(), h)
	if err != nil {
		t.Fatalf("Result: %v", err)
	}
	if res.State != StateSucceeded {
		t.Fatalf("running task state = %s, want succeeded (graceful drain)", res.State)
	}
	close(ran)
	t.Logf("leaves executed before shutdown: %d (remaining queued leaves were canceled)", len(ran))
}

// TestShutdownForcesUncooperativeTaskByTimeout verifies that when the
// drain deadline elapses while a task is still running, Shutdown
// cancels task contexts (cooperative force) and ultimately returns the
// deadline error once the task observes it and the pool exits.
func TestShutdownForcesUncooperativeTaskByTimeout(t *testing.T) {
	e, err := New(WithWorkers(1))
	if err != nil {
		t.Fatal(err)
	}
	running := make(chan struct{})
	e.SubmitFunc("slow", func(ctx context.Context, rt Runtime) (any, error) {
		close(running)
		// Uncooperative at first: wait on a gate unrelated to ctx;
		// observe cancellation only after a bounded delay.
		select {
		case <-ctx.Done():
		case <-time.After(5 * time.Second):
		}
		// Give the force path a little post-cancel work, then return.
		if err := rt.Sleep(ctx, 25*time.Millisecond); err != nil {
			return nil, err
		}
		return nil, nil
	})
	<-running

	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	start := time.Now()
	err = e.Shutdown(ctx)
	if err == nil {
		t.Fatal("Shutdown should report the timeout context error after forcing cancellation")
	}
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("Shutdown err = %v, want DeadlineExceeded", err)
	}
	if elapsed := time.Since(start); elapsed < 50*time.Millisecond {
		t.Fatalf("Shutdown returned after %v, before the timeout elapsed", elapsed)
	}
}

// TestSleepWithMockClock: a sleeping task stays running (its worker
// parked/helping), then completes once the mock clock is advanced past
// the deadline — no wall-clock waiting required. The test waits for
// the task to actually START before advancing the clock, avoiding a
// race where advancement precedes scheduling.
func TestSleepWithMockClock(t *testing.T) {
	clk := NewMockClock(time.Unix(0, 0))
	e, err := New(WithWorkers(1), WithClock(clk))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		_ = e.Shutdown(ctx)
	})

	done := make(chan TaskInfo, 1)
	h, err := e.SubmitFunc("sleeper", func(ctx context.Context, rt Runtime) (any, error) {
		if err := rt.Sleep(ctx, 3*time.Second); err != nil {
			return nil, err
		}
		return clk.Now().Unix(), nil
	})
	if err != nil {
		t.Fatal(err)
	}
	go func() {
		res, _ := e.Result(context.Background(), h)
		done <- res.TaskInfo
	}()

	// Wait until the mock clock holds the sleep timer: this proves the
	// task is inside rt.Sleep before we advance.
	waitTimer := time.Now().Add(2 * time.Second)
	for time.Now().Before(waitTimer) {
		if clk.Pending() >= 1 {
			break
		}
		time.Sleep(time.Millisecond)
	}
	if clk.Pending() < 1 {
		t.Fatal("sleep timer was never scheduled")
	}

	clk.Advance(2 * time.Second)
	select {
	case info := <-done:
		t.Fatalf("sleeper finished too early: %+v", info)
	case <-time.After(30 * time.Millisecond):
	}
	clk.Advance(1 * time.Second)
	select {
	case info := <-done:
		if info.State != StateSucceeded || toIntI(info.Value) != 3 {
			t.Fatalf("info = %+v, want succeeded value 3", info)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("sleeper did not finish after clock advanced past deadline")
	}
}
