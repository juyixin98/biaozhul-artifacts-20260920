package dynpool

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func waitFor(t *testing.T, timeout time.Duration, cond func() bool, msg string) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatalf("timed out waiting for: %s", msg)
}

// blockTask is a cooperative blocking task: it parks until release is closed
// or ctx is cancelled. It records start and completion exactly once.
type blockTask struct {
	id          string
	started     chan struct{}
	release     chan struct{}
	starts      atomic.Int64
	completions atomic.Int64
	cancelled   atomic.Bool
}

func newBlockTask(id string) *blockTask {
	return &blockTask{id: id, started: make(chan struct{}), release: make(chan struct{})}
}

func (b *blockTask) fn(ctx context.Context) error {
	b.starts.Add(1)
	close(b.started)
	select {
	case <-releaseChan(b):
		return nil
	case <-ctx.Done():
		b.cancelled.Store(true)
		return ctx.Err()
	}
}

func releaseChan(b *blockTask) <-chan struct{} { return b.release }

func (b *blockTask) doneOnce() {
	b.completions.Add(1)
}

// TestBasicExecutesTasks verifies tasks run and complete exactly once.
func TestBasicExecutesTasks(t *testing.T) {
	sink := &MemorySink{}
	p, err := New(Config{Name: "t1", Workers: 2, QueueSize: 16, Sink: sink})
	if err != nil {
		t.Fatal(err)
	}
	var ran atomic.Int64
	for i := 0; i < 10; i++ {
		if _, err := p.Submit(func(context.Context) error {
			ran.Add(1)
			time.Sleep(time.Millisecond)
			return nil
		}); err != nil {
			t.Fatal(err)
		}
	}
	if err := p.Shutdown(context.Background()); err != nil {
		t.Fatal(err)
	}
	if ran.Load() != 10 {
		t.Fatalf("ran=%d, want 10", ran.Load())
	}
	if got := p.Status().CompletedTasks; got != 10 {
		t.Fatalf("completed=%d, want 10", got)
	}
	var completes int
	for _, e := range sink.Events() {
		if e.Type == EventTaskCompleted {
			completes++
		}
	}
	if completes != 10 {
		t.Fatalf("task.completed events=%d, want 10", completes)
	}
}

// TestQueueBoundReject verifies the bounded queue rejects with PolicyAbort.
func TestQueueBoundReject(t *testing.T) {
	p, _ := New(Config{Workers: 1, QueueSize: 1, Reject: PolicyAbort})
	t1 := newBlockTask("t1")
	t2 := newBlockTask("t2")
	t3 := newBlockTask("t3")
	h1, err := p.Submit(t1.fn)
	if err != nil {
		t.Fatal(err)
	}
	<-t1.started // worker is occupied
	h2, err := p.Submit(t2.fn)
	if err != nil {
		t.Fatal(err)
	}
	waitFor(t, time.Second, func() bool { return p.Status().Queued == 1 }, "t2 queued")
	h3, err := p.Submit(t3.fn)
	if !errors.Is(err, ErrTaskRejected) {
		t.Fatalf("want ErrTaskRejected, got %v", err)
	}
	// Rejected task's handle is finished immediately and never runs.
	if st := p.Status().RejectedTasks; st != 1 {
		t.Fatalf("rejected=%d want 1", st)
	}
	if h3 == nil {
		t.Fatal("rejected task returned nil handle")
	}
	if !h3.IsDone() {
		t.Fatal("rejected task handle should be done immediately")
	}
	if ran, _ := h3.Result(); ran {
		t.Fatal("rejected task must never run")
	}
	close(t1.release)
	close(t2.release)
	h1.Result()
	h2.Result()
	if err := p.Shutdown(context.Background()); err != nil {
		t.Fatal(err)
	}
}

// TestShrinkWaitsForRunningTask is the core shrink acceptance: shrinking while
// a blocking task runs must not interrupt it; once released the worker exits
// cleanly and no worker leaks.
func TestShrinkWaitsForRunningTask(t *testing.T) {
	exec := &TrackedExecutor{}
	sink := &MemorySink{}
	p, _ := New(Config{Name: "shrink", Workers: 3, QueueSize: 8, Sink: sink, Executor: exec})
	waitFor(t, time.Second, func() bool { return p.Status().ActiveWorkers == 3 }, "3 workers up")

	b1, b2, b3 := newBlockTask("b1"), newBlockTask("b2"), newBlockTask("b3")
	h1, _ := p.Submit(b1.fn)
	h2, _ := p.Submit(b2.fn)
	h3, _ := p.Submit(b3.fn)
	<-b1.started
	<-b2.started
	<-b3.started

	if err := p.Resize(1); err != nil {
		t.Fatal(err)
	}
	// Two workers are retiring but all three are inside blocking tasks; none
	// may exit while the tasks block.
	waitFor(t, time.Second, func() bool { return p.Status().Retiring == 2 }, "2 retiring")
	time.Sleep(20 * time.Millisecond)
	if p.Status().ActiveWorkers != 3 {
		t.Fatalf("active=%d, want 3 (running tasks must not be interrupted)", p.Status().ActiveWorkers)
	}
	close(b1.release)
	close(b2.release)
	close(b3.release)
	h1.Result()
	h2.Result()
	h3.Result()

	// After the in-flight tasks finish, two retirees drain queue (empty) and
	// leave; exactly one worker remains.
	waitFor(t, time.Second, func() bool { return p.Status().ActiveWorkers == 1 }, "1 worker left after shrink")
	if err := p.Shutdown(context.Background()); err != nil {
		t.Fatal(err)
	}
	if !exec.WaitActive(time.Second) {
		t.Fatalf("worker goroutine leak: %d still active", exec.Active())
	}
}

// TestShrinkDoesNotLoseQueuedTasks proves a shrinking worker drains queued
// tasks before exiting: every accepted task completes, none twice.
func TestShrinkDoesNotLoseQueuedTasks(t *testing.T) {
	exec := &TrackedExecutor{}
	p, _ := New(Config{Name: "drain", Workers: 4, QueueSize: 64, Executor: exec})
	waitFor(t, time.Second, func() bool { return p.Status().ActiveWorkers == 4 }, "4 up")

	// Saturate all 4 workers with blocking tasks.
	blockers := make([]*blockTask, 4)
	for i := range blockers {
		blockers[i] = newBlockTask(fmt.Sprintf("blk%d", i))
		if _, err := p.Submit(blockers[i].fn); err != nil {
			t.Fatal(err)
		}
	}
	for _, b := range blockers {
		<-b.started
	}

	// Queue 20 fast tasks.
	const N = 20
	var starts, completes atomic.Int64
	var mu sync.Mutex
	seen := map[int64]struct{}{}
	handles := make([]*Handle, N)
	for i := 0; i < N; i++ {
		i := i
		h, err := p.Submit(func(context.Context) error {
			starts.Add(1)
			time.Sleep(2 * time.Millisecond)
			mu.Lock()
			if _, dup := seen[int64(i)]; dup {
				mu.Unlock()
				t.Errorf("task %d executed twice", i)
				return nil
			}
			seen[int64(i)] = struct{}{}
			mu.Unlock()
			completes.Add(1)
			return nil
		})
		if err != nil {
			t.Fatal(err)
		}
		handles[i] = h
	}
	if p.Status().Queued != N {
		t.Fatalf("queue=%d want %d", p.Status().Queued, N)
	}

	// Interleave a shrink to zero with the workload, then release blockers.
	if err := p.Resize(0); err != nil {
		t.Fatal(err)
	}
	waitFor(t, time.Second, func() bool { return p.Status().Retiring == 4 }, "all 4 retiring")
	for _, b := range blockers {
		close(b.release)
	}
	// Graceful shutdown: drainer logic not needed because retiring workers
	// drain the queue themselves.
	if err := p.Shutdown(context.Background()); err != nil {
		t.Fatal(err)
	}
	for i, h := range handles {
		ran, _ := h.Result()
		if !ran {
			t.Fatalf("queued task %d never ran after shrink", i)
		}
	}
	if starts.Load() != N || completes.Load() != N {
		t.Fatalf("starts=%d completes=%d want %d/%d (no loss, no dup)", starts.Load(), completes.Load(), N, N)
	}
	if !exec.WaitActive(time.Second) {
		t.Fatalf("worker leak: %d active", exec.Active())
	}
}

// TestInterleavedSubmitResizeShutdown is the main acceptance test: many
// goroutines submit while resizes and a final shutdown happen concurrently.
// Every accepted task must complete exactly once; rejected tasks never run.
func TestInterleavedSubmitResizeShutdown(t *testing.T) {
	exec := &TrackedExecutor{}
	sink := &MemorySink{}
	p, _ := New(Config{Name: "interleave", Workers: 2, QueueSize: 16, Reject: PolicyAbort, Executor: exec, Sink: sink})

	const submitters = 8
	const each = 100
	var accepted, rejected atomic.Int64
	var executed atomic.Int64
	var execMu sync.Mutex
	execSeen := map[string]int{}

	var wg sync.WaitGroup
	for i := 0; i < submitters; i++ {
		i := i
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < each; j++ {
				id := fmt.Sprintf("g%d-%d", i, j)
				h, err := p.Submit(func(context.Context) error {
					time.Sleep(time.Microsecond)
					execMu.Lock()
					execSeen[id]++
					execMu.Unlock()
					executed.Add(1)
					return nil
				}, WithTaskID(id))
				if err != nil {
					rejected.Add(1)
					continue
				}
				accepted.Add(1)
				// Track eventual outcome but don't block the submitter.
				go func() { _, _ = h.Result() }()
			}
		}()
	}

	// Resize up and down repeatedly while submissions fly.
	var rwg sync.WaitGroup
	rwg.Add(1)
	go func() {
		defer rwg.Done()
		for _, n := range []int{8, 1, 6, 0, 4, 2} {
			if p.Status().State != "running" {
				return
			}
			_ = p.Resize(n)
			time.Sleep(5 * time.Millisecond)
		}
	}()

	wg.Wait()
	rwg.Wait()
	if err := p.Shutdown(context.Background()); err != nil {
		t.Fatal(err)
	}

	if got := executed.Load(); got != accepted.Load() {
		t.Fatalf("executed=%d accepted=%d (accepted tasks must not be lost)", got, accepted.Load())
	}
	for id, n := range execSeen {
		if n != 1 {
			t.Fatalf("task %s completed %d times, want exactly 1", id, n)
		}
	}
	if accepted.Load()+rejected.Load() != submitters*each {
		t.Fatalf("accepted+rejected=%d want %d", accepted.Load()+rejected.Load(), submitters*each)
	}
	if !exec.WaitActive(2 * time.Second) {
		t.Fatalf("worker leak after shutdown: %d active", exec.Active())
	}
	// Final pool event must be pool.stopped.
	evs := sink.Events()
	if last := evs[len(evs)-1]; last.Type != EventPoolStopped {
		t.Fatalf("last event=%s want %s", last.Type, EventPoolStopped)
	}
}

// TestGracefulShutdownDrainsQueued verifies Shutdown semantics directly.
func TestGracefulShutdownDrainsQueued(t *testing.T) {
	p, _ := New(Config{Workers: 1, QueueSize: 32})
	b := newBlockTask("b")
	if _, err := p.Submit(b.fn); err != nil {
		t.Fatal(err)
	}
	<-b.started
	var ran atomic.Int64
	for i := 0; i < 10; i++ {
		if _, err := p.Submit(func(context.Context) error { ran.Add(1); return nil }); err != nil {
			t.Fatal(err)
		}
	}
	// Submissions during shutdown fail fast.
	shutdownDone := make(chan error, 1)
	go func() { shutdownDone <- p.Shutdown(context.Background()) }()
	waitFor(t, time.Second, func() bool { return p.Status().State == "shutting_down" }, "shutting down")
	if _, err := p.Submit(func(context.Context) error { return nil }); !errors.Is(err, ErrPoolShuttingDown) {
		t.Fatalf("submit during shutdown: got %v", err)
	}
	if err := p.Resize(5); !errors.Is(err, ErrPoolShuttingDown) {
		t.Fatalf("resize during shutdown: got %v", err)
	}
	close(b.release)
	if err := <-shutdownDone; err != nil {
		t.Fatal(err)
	}
	if ran.Load() != 10 {
		t.Fatalf("queued tasks ran=%d want 10", ran.Load())
	}
	if p.Status().State != "stopped" {
		t.Fatalf("state=%s want stopped", p.Status().State)
	}
	// Idempotency.
	if err := p.Shutdown(context.Background()); err != nil {
		t.Fatalf("second shutdown: %v", err)
	}
}

// TestGracefulShutdownZeroWorkers covers the drainer path: target=0 with
// queued tasks when Shutdown is called must still execute everything.
func TestGracefulShutdownZeroWorkers(t *testing.T) {
	exec := &TrackedExecutor{}
	p, _ := New(Config{Workers: 2, QueueSize: 16, Executor: exec})
	waitFor(t, time.Second, func() bool { return p.Status().ActiveWorkers == 2 }, "workers up")
	if err := p.Resize(0); err != nil {
		t.Fatal(err)
	}
	waitFor(t, time.Second, func() bool { return p.Status().ActiveWorkers == 0 }, "shrunk to 0")

	var ran atomic.Int64
	for i := 0; i < 5; i++ {
		if _, err := p.Submit(func(context.Context) error { ran.Add(1); return nil }); err != nil {
			t.Fatal(err)
		}
	}
	if err := p.Shutdown(context.Background()); err != nil {
		t.Fatal(err)
	}
	if ran.Load() != 5 {
		t.Fatalf("drainer ran=%d want 5", ran.Load())
	}
	if !exec.WaitActive(time.Second) {
		t.Fatal("drainer goroutine leaked")
	}
}

// TestShutdownNowCancelsAndDrops verifies force semantics: running tasks get
// ctx cancellation, queued tasks are returned and never run.
func TestShutdownNowCancelsAndDrops(t *testing.T) {
	exec := &TrackedExecutor{}
	p, _ := New(Config{Workers: 2, QueueSize: 32, Executor: exec})
	waitFor(t, time.Second, func() bool { return p.Status().ActiveWorkers == 2 }, "workers up")

	b1, b2 := newBlockTask("b1"), newBlockTask("b2")
	h1, _ := p.Submit(b1.fn)
	h2, _ := p.Submit(b2.fn)
	<-b1.started
	<-b2.started

	var queuedRan atomic.Int64
	qh := make([]*Handle, 6)
	for i := range qh {
		var err error
		qh[i], err = p.Submit(func(context.Context) error { queuedRan.Add(1); return nil })
		if err != nil {
			t.Fatal(err)
		}
	}
	waitFor(t, time.Second, func() bool { return p.Status().Queued == 6 }, "6 tasks queued")

	dropped := p.ShutdownNow()
	if len(dropped) != 6 {
		t.Fatalf("dropped=%v, want 6 ids", dropped)
	}
	if queuedRan.Load() != 0 {
		t.Fatalf("queued tasks ran=%d, want 0", queuedRan.Load())
	}
	for i, h := range qh {
		ran, _ := h.Result()
		if ran {
			t.Fatalf("queued handle %d reports ran", i)
		}
	}
	for i, h := range []*Handle{h1, h2} {
		ran, err := h.Result()
		if !ran {
			t.Fatalf("running handle %d should have run", i)
		}
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("running task %d err=%v want context.Canceled", i, err)
		}
	}
	if !b1.cancelled.Load() || !b2.cancelled.Load() {
		t.Fatal("blocking tasks did not observe cancellation")
	}
	st := p.Status()
	if st.CancelledTasks != 2 {
		t.Fatalf("cancelled=%d want 2", st.CancelledTasks)
	}
	if st.DroppedTasks != 6 {
		t.Fatalf("dropped=%d want 6", st.DroppedTasks)
	}
	if st.State != "stopped" {
		t.Fatalf("state=%s", st.State)
	}
	if !exec.WaitActive(time.Second) {
		t.Fatal("goroutine leak after ShutdownNow")
	}
	// Idempotent: second call returns nil and does not panic.
	if got := p.ShutdownNow(); got != nil {
		t.Fatalf("second ShutdownNow=%v want nil", got)
	}
	// Submit/Resize after full stop.
	if _, err := p.Submit(func(context.Context) error { return nil }); !errors.Is(err, ErrPoolStopped) {
		t.Fatalf("submit after stop: %v", err)
	}
	if err := p.Resize(3); !errors.Is(err, ErrPoolStopped) {
		t.Fatalf("resize after stop: %v", err)
	}
}

// TestGracePeriodTimeout verifies Shutdown respects GracePeriod but still
// reaches stopped once the blocker clears.
func TestGracePeriodTimeout(t *testing.T) {
	p, _ := New(Config{Workers: 1, QueueSize: 2, GracePeriod: 50 * time.Millisecond})
	b := newBlockTask("b")
	if _, err := p.Submit(b.fn); err != nil {
		t.Fatal(err)
	}
	<-b.started
	err := p.Shutdown(context.Background())
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("shutdown err=%v want DeadlineExceeded", err)
	}
	close(b.release)
	waitFor(t, time.Second, func() bool { return p.Status().State == "stopped" }, "eventual stop")
}

// TestShutdownNowDropRace stresses the window where workers race a queued
// receive against context cancellation: no task may be lost or double-counted.
// Invariant: initial 4 blocked + 50 queued == running(cancelled 4) + dropped.
func TestShutdownNowDropRace(t *testing.T) {
	exec := &TrackedExecutor{}
	for iter := 0; iter < 100; iter++ {
		p, _ := New(Config{Name: fmt.Sprintf("fr%d", iter), Workers: 4, QueueSize: 128, Executor: exec})
		waitFor(t, time.Second, func() bool { return p.Status().ActiveWorkers == 4 }, "4 up")
		blockers := make([]*blockTask, 4)
		for i := range blockers {
			blockers[i] = newBlockTask(fmt.Sprintf("frblk%d-%d", iter, i))
			if _, err := p.Submit(blockers[i].fn); err != nil {
				t.Fatal(err)
			}
		}
		for _, b := range blockers {
			<-b.started
		}
		var ranQueued atomic.Int64
		for i := 0; i < 50; i++ {
			if _, err := p.Submit(func(context.Context) error { ranQueued.Add(1); return nil }); err != nil {
				t.Fatal(err)
			}
		}
		dropped := p.ShutdownNow()
		st := p.Status()
		// Every queued task is either dropped without running or ran; 4 running
		// blockers were cancelled.
		if int64(len(dropped)) != st.DroppedTasks {
			t.Fatalf("iter %d: returned dropped=%d counter=%d", iter, len(dropped), st.DroppedTasks)
		}
		if st.DroppedTasks+ranQueued.Load() != 50 {
			t.Fatalf("iter %d: dropped=%d ranQueued=%d, want sum 50", iter, st.DroppedTasks, ranQueued.Load())
		}
		if st.CancelledTasks != 4 {
			t.Fatalf("iter %d: cancelled=%d want 4", iter, st.CancelledTasks)
		}
		if st.State != "stopped" {
			t.Fatalf("iter %d: state=%s", iter, st.State)
		}
	}
	if !exec.WaitActive(2 * time.Second) {
		t.Fatalf("goroutine leak: %d", exec.Active())
	}
}

// TestGracefulThenForceEscalation verifies ShutdownNow escalates a pending
// graceful shutdown and the terminal event is pool.force_stopped.
func TestGracefulThenForceEscalation(t *testing.T) {
	sink := &MemorySink{}
	p, _ := New(Config{Workers: 1, QueueSize: 4, Sink: sink})
	b := newBlockTask("esc")
	h, err := p.Submit(b.fn)
	if err != nil {
		t.Fatal(err)
	}
	<-b.started

	shDone := make(chan error, 1)
	go func() { shDone <- p.Shutdown(context.Background()) }()
	waitFor(t, time.Second, func() bool { return p.Status().State == "shutting_down" }, "shutting down")
	dropped := p.ShutdownNow() // escalate
	if err := <-shDone; !errors.Is(err, ErrPoolShuttingDown) {
		t.Fatalf("graceful waiter got %v want ErrPoolShuttingDown", err)
	}
	if len(dropped) != 0 {
		t.Fatalf("dropped=%v", dropped)
	}
	if ran, err := h.Result(); !ran || !errors.Is(err, context.Canceled) {
		t.Fatalf("blocker ran=%v err=%v", ran, err)
	}
	if got := p.Status(); got.State != "stopped" || got.CancelledTasks != 1 {
		t.Fatalf("status=%+v", got)
	}
	var hasForceStop bool
	for _, e := range sink.Events() {
		if e.Type == EventPoolForceStop {
			hasForceStop = true
		}
	}
	if !hasForceStop {
		t.Fatal("missing pool.force_stopped event after escalation")
	}
}

// workers, then Submit and Shutdown interleave. Every accepted task must be
// accounted for (executed by the shutdown drainer) — Shutdown must never
// return stopped with a task still stranded in the queue.
func TestSubmitShutdownRaceNoStrandedTask(t *testing.T) {
	exec := &TrackedExecutor{}
	for iter := 0; iter < 50; iter++ {
		p, _ := New(Config{Name: fmt.Sprintf("race%d", iter), Workers: 1, QueueSize: 64, Executor: exec})
		waitFor(t, time.Second, func() bool { return p.Status().ActiveWorkers == 1 }, "worker up")
		if err := p.Resize(0); err != nil {
			t.Fatal(err)
		}
		waitFor(t, time.Second, func() bool { return p.Status().ActiveWorkers == 0 }, "zero workers")

		var accepted, executed atomic.Int64
		var wg sync.WaitGroup
		for g := 0; g < 4; g++ {
			wg.Add(1)
			go func() {
				defer wg.Done()
				for i := 0; i < 25; i++ {
					h, err := p.Submit(func(context.Context) error { executed.Add(1); return nil })
					if err == nil {
						accepted.Add(1)
						go func() { _, _ = h.Result() }()
					}
				}
			}()
		}
		if err := p.Shutdown(context.Background()); err != nil {
			t.Fatalf("iter %d: %v", iter, err)
		}
		wg.Wait()
		if got := executed.Load(); got != accepted.Load() {
			t.Fatalf("iter %d: accepted=%d executed=%d (stranded task)", iter, accepted.Load(), got)
		}
	}
	if !exec.WaitActive(2 * time.Second) {
		t.Fatalf("goroutine leak: %d", exec.Active())
	}
}
