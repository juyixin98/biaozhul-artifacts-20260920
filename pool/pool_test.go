package pool

import (
	"context"
	"errors"
	"fmt"
	"runtime"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// waitFor polls cond until it returns true or the timeout elapses.
func waitFor(timeout time.Duration, cond func() bool) bool {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if cond() {
			return true
		}
		time.Sleep(time.Millisecond)
	}
	return cond()
}

// blockFirstTask saturates one worker with a task that blocks until the
// returned channel is closed, returning the channel and the task future.
func blockFirstTask(t *testing.T, p *Pool) (*Future, chan struct{}) {
	t.Helper()
	block := make(chan struct{})
	f, err := p.Submit(Task{Fn: func(ctx context.Context) (any, error) {
		<-block
		return nil, nil
	}})
	if err != nil {
		t.Fatalf("blocking submit: %v", err)
	}
	if !waitFor(time.Second, func() bool { return p.Stats().RunningNow == 1 }) {
		t.Fatal("blocking task never became running")
	}
	return f, block
}

func TestBasicExecuteAndExactlyOnce(t *testing.T) {
	rec := NewEventRecorder(256)
	p, err := New(Config{Name: "t", Workers: 3, QueueCapacity: 64, Sink: rec})
	if err != nil {
		t.Fatal(err)
	}
	const n = 50
	var runs int64
	futs := make([]*Future, n)
	for i := 0; i < n; i++ {
		i := i
		futs[i], err = p.Submit(Task{Type: "inc", Fn: func(ctx context.Context) (any, error) {
			atomic.AddInt64(&runs, 1)
			return i, nil
		}})
		if err != nil {
			t.Fatalf("submit %d: %v", i, err)
		}
	}
	for i, f := range futs {
		v, err := f.Get(context.Background())
		if err != nil {
			t.Fatalf("get %d: %v", i, err)
		}
		if v.(int) != i {
			t.Fatalf("task %d got result %v", i, v)
		}
	}
	if runs != n {
		t.Fatalf("runs=%d want %d", runs, n)
	}
	if err := p.Shutdown(context.Background()); err != nil {
		t.Fatal(err)
	}
	st := p.Stats()
	if st.Completed != n {
		t.Fatalf("completed=%d want %d", st.Completed, n)
	}
	if !p.Terminated() {
		t.Fatal("not terminated")
	}
}

func TestNoDuplicateCompletion(t *testing.T) {
	p, _ := New(Config{Workers: 4, QueueCapacity: 512})
	const n = 500
	var settled int64
	var started int64
	futs := make([]*Future, n)
	var wg sync.WaitGroup
	wg.Add(n)
	for i := 0; i < n; i++ {
		i := i
		var err error
		futs[i], err = p.Submit(Task{Fn: func(ctx context.Context) (any, error) {
			atomic.AddInt64(&started, 1)
			if i%7 == 0 {
				return nil, errors.New("boom")
			}
			return i, nil
		}})
		if err != nil {
			t.Fatal(err)
		}
		futs[i].OnSettle(func(f *Future) {
			atomic.AddInt64(&settled, 1)
			wg.Done()
		})
	}
	wg.Wait()
	if started != n || settled != n {
		t.Fatalf("started=%d settled=%d want %d", started, settled, n)
	}
	counts := map[string]int{}
	for _, f := range futs {
		s := f.Snapshot()
		counts[s.State]++
		if s.FinishedAt.IsZero() {
			t.Fatal("zero finish")
		}
	}
	// i%7==0 for i in 0..n-1: values 0,7,...,497 -> (n-1)/7+1 failures.
	wantFail := (n-1)/7 + 1
	if counts["completed"] != n-wantFail || counts["failed"] != wantFail {
		t.Fatalf("counts=%v want failed=%d", counts, wantFail)
	}
	if err := p.Shutdown(context.Background()); err != nil {
		t.Fatal(err)
	}
}

func TestShrinkWaitsForRunningTasks(t *testing.T) {
	rec := NewEventRecorder(1024)
	p, _ := New(Config{Name: "shrink", Workers: 4, QueueCapacity: 100, Sink: rec})

	started := make(chan int, 8)
	release := make(chan struct{})
	var done int64
	// Saturate: 8 blocking tasks. 4 run, 4 queue.
	futs := make([]*Future, 8)
	for i := 0; i < 8; i++ {
		i := i
		f, err := p.Submit(Task{ID: fmt.Sprintf("blk-%d", i), Fn: func(ctx context.Context) (any, error) {
			started <- i
			<-release
			atomic.AddInt64(&done, 1)
			return i, nil
		}})
		if err != nil {
			t.Fatal(err)
		}
		futs[i] = f
	}
	got := map[int]bool{}
	for i := 0; i < 4; i++ {
		got[<-started] = true
	}
	if !waitFor(time.Second, func() bool { return p.Stats().RunningNow == 4 }) {
		t.Fatal("tasks never became running")
	}

	// Shrink to 1 while tasks are blocked: no worker may die mid-task.
	if err := p.Resize(1); err != nil {
		t.Fatal(err)
	}
	time.Sleep(50 * time.Millisecond)
	if p.Stats().RunningNow != 4 {
		t.Fatalf("running changed during shrink: %d", p.Stats().RunningNow)
	}
	if d := atomic.LoadInt64(&done); d != 0 {
		t.Fatalf("tasks completed early: %d", d)
	}

	// Release everything: all 8 must complete, exactly once, with the pool
	// eventually running at size 1.
	close(release)
	for _, f := range futs {
		if _, err := f.Get(context.Background()); err != nil {
			t.Fatal(err)
		}
	}
	if done != 8 {
		t.Fatalf("done=%d want 8", done)
	}
	if !waitFor(2*time.Second, func() bool { return p.Stats().ActiveWorkers == 1 }) {
		t.Fatalf("active=%d want 1 after shrink settled", p.Stats().ActiveWorkers)
	}
	if err := p.Shutdown(context.Background()); err != nil {
		t.Fatal(err)
	}
	// Event sanity: exactly the 4 started workers stop, each with a valid
	// reason. The shrink/drain split is timing dependent (a worker can be
	// idle and retired, or still draining at shutdown), so only the total
	// and reasons are asserted.
	var shrinks, drained, forced, stops int
	for _, e := range rec.Events() {
		if e.Kind == EventWorkerStopped {
			stops++
			switch e.Reason {
			case "shrink":
				shrinks++
			case "drained":
				drained++
			case "force_canceled":
				forced++
			}
		}
	}
	if stops != 4 || forced != 0 || shrinks+drained != 4 {
		t.Fatalf("worker stops: total=%d shrink=%d drained=%d forced=%d",
			stops, shrinks, drained, forced)
	}
}

func TestGrowThenShrinkCoalescing(t *testing.T) {
	p, _ := New(Config{Workers: 2, QueueCapacity: 100})
	if err := p.Resize(5); err != nil {
		t.Fatal(err)
	}
	if !waitFor(time.Second, func() bool { return p.Stats().ActiveWorkers == 5 }) {
		t.Fatal("grow did not materialize")
	}
	// Shrink then immediately grow: tokens should be reclaimed, no churn.
	if err := p.Resize(1); err != nil {
		t.Fatal(err)
	}
	if err := p.Resize(5); err != nil {
		t.Fatal(err)
	}
	// A worker may consume a shrink notice (and exit) between the two
	// resizes; the grow then spawns a replacement. Wait for convergence
	// rather than asserting an instantaneous value.
	if !waitFor(2*time.Second, func() bool { return p.Stats().ActiveWorkers == 5 }) {
		t.Fatalf("after resize 1->5 active=%d", p.Stats().ActiveWorkers)
	}
	// Some workers may have consumed a token before the grow; capacity must
	// still converge and work run.
	var ran int64
	for i := 0; i < 30; i++ {
		f, _ := p.Submit(Task{Fn: func(ctx context.Context) (any, error) {
			atomic.AddInt64(&ran, 1)
			return nil, nil
		}})
		if _, err := f.Get(context.Background()); err != nil {
			t.Fatal(err)
		}
	}
	if ran != 30 {
		t.Fatalf("ran=%d", ran)
	}
	if err := p.Shutdown(context.Background()); err != nil {
		t.Fatal(err)
	}
}

func TestGracefulShutdownDrainsAllAccepted(t *testing.T) {
	p, _ := New(Config{Workers: 2, QueueCapacity: 200})
	const n = 100
	gate := make(chan struct{})
	futs := make([]*Future, n)
	for i := 0; i < n; i++ {
		f, err := p.Submit(Task{Fn: func(ctx context.Context) (any, error) {
			<-gate
			return 1, nil
		}})
		if err != nil {
			t.Fatal(err)
		}
		futs[i] = f
	}
	shutdownDone := make(chan error, 1)
	go func() { shutdownDone <- p.Shutdown(context.Background()) }()
	// New submissions are rejected once shutdown starts. Poll with a task
	// we only submit after the state has flipped, and drain its outcome.
	if !waitFor(time.Second, func() bool { return p.Stats().State != "running" }) {
		t.Fatal("shutdown never started")
	}
	if _, err := p.Submit(Task{Fn: func(context.Context) (any, error) { return nil, nil }}); !errors.Is(err, ErrPoolShuttingDown) {
		t.Fatalf("submission during shutdown err=%v want ErrPoolShuttingDown", err)
	}
	close(gate)
	select {
	case err := <-shutdownDone:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("graceful shutdown did not finish")
	}
	for i, f := range futs {
		if st := f.Snapshot(); st.State != "completed" {
			t.Fatalf("task %d state=%s", i, st.State)
		}
	}
	if st := p.Stats(); st.Completed != n {
		t.Fatalf("completed=%d want %d", st.Completed, n)
	}
}

func TestGracefulDrainAfterResizeZero(t *testing.T) {
	p, _ := New(Config{Workers: 3, QueueCapacity: 50})
	if err := p.Resize(0); err != nil {
		t.Fatal(err)
	}
	if !waitFor(time.Second, func() bool { return p.Stats().ActiveWorkers == 0 }) {
		t.Fatal("resize to 0 did not retire all")
	}
	f, err := p.Submit(Task{Fn: func(ctx context.Context) (any, error) { return 42, nil }})
	if err != nil {
		t.Fatal(err)
	}
	if err := p.Shutdown(context.Background()); err != nil {
		t.Fatal(err)
	}
	v, err := f.Get(context.Background())
	if err != nil || v.(int) != 42 {
		t.Fatalf("queued task lost: v=%v err=%v", v, err)
	}
}

func TestShutdownNowCancelsPendingNotRunning(t *testing.T) {
	p, _ := New(Config{Workers: 1, QueueCapacity: 50})
	sawCancel := make(chan struct{})
	blocking, _ := p.Submit(Task{Fn: func(ctx context.Context) (any, error) {
		<-ctx.Done()
		close(sawCancel)
		return nil, ctx.Err()
	}})
	var queued []*Future
	for i := 0; i < 10; i++ {
		f, err := p.Submit(Task{Fn: func(ctx context.Context) (any, error) {
			t.Error("queued task should never run")
			return nil, nil
		}})
		if err != nil {
			t.Fatal(err)
		}
		queued = append(queued, f)
	}
	if !waitFor(time.Second, func() bool { return p.Stats().RunningNow == 1 }) {
		t.Fatal("blocking task not running")
	}
	notRun, err := p.ShutdownNow(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	select {
	case <-sawCancel:
	case <-time.After(2 * time.Second):
		t.Fatal("running task ctx was not canceled")
	}
	if len(notRun) != 10 {
		t.Fatalf("notRun=%d want 10", len(notRun))
	}
	for _, f := range queued {
		if st := f.Snapshot(); st.State != "canceled" {
			t.Fatalf("queued state=%s want canceled", st.State)
		}
	}
	// The running task cooperatively stopped; it settles as failed (ctx err)
	// rather than canceled, because it did start.
	if st := blocking.Snapshot(); st.State != "failed" {
		t.Fatalf("running task state=%s want failed", st.State)
	}
}

func TestShutdownNowRunningTaskIgnoresContext(t *testing.T) {
	p, _ := New(Config{Workers: 1, QueueCapacity: 10})
	release := make(chan struct{})
	f, _ := p.Submit(Task{Fn: func(ctx context.Context) (any, error) {
		<-release // ignores ctx; must still be allowed to finish
		return "done", nil
	}})
	if !waitFor(time.Second, func() bool { return p.Stats().RunningNow == 1 }) {
		t.Fatal("not running")
	}
	shutdownReturned := make(chan error, 1)
	go func() {
		_, err := p.ShutdownNow(context.Background())
		shutdownReturned <- err
	}()
	select {
	case <-shutdownReturned:
		t.Fatal("ShutdownNow returned before task finished")
	case <-time.After(100 * time.Millisecond):
	}
	close(release)
	select {
	case err := <-shutdownReturned:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("ShutdownNow stuck")
	}
	v, err := f.Get(context.Background())
	if err != nil || v != "done" {
		t.Fatalf("v=%v err=%v", v, err)
	}
}

func TestRejectPolicies(t *testing.T) {
	t.Run("abort", func(t *testing.T) {
		p, _ := New(Config{Workers: 1, QueueCapacity: 1, Policy: RejectAbort})
		_, block := blockFirstTask(t, p)
		_, _ = p.Submit(Task{Fn: func(context.Context) (any, error) { return nil, nil }})
		_, err := p.Submit(Task{Fn: func(context.Context) (any, error) { return nil, nil }})
		if !errors.Is(err, ErrQueueFull) {
			t.Fatalf("err=%v", err)
		}
		close(block)
		if err := p.Shutdown(context.Background()); err != nil {
			t.Fatal(err)
		}
	})

	t.Run("discard", func(t *testing.T) {
		p, _ := New(Config{Workers: 1, QueueCapacity: 1, Policy: RejectDiscard})
		_, block := blockFirstTask(t, p)
		fKeep, _ := p.Submit(Task{Fn: func(context.Context) (any, error) { return "keep", nil }})
		fDrop, err := p.Submit(Task{Fn: func(context.Context) (any, error) { return "drop", nil }})
		if !errors.Is(err, ErrTaskDiscarded) {
			t.Fatalf("err=%v", err)
		}
		if fDrop.Snapshot().State != "rejected" {
			t.Fatalf("drop state=%s", fDrop.Snapshot().State)
		}
		close(block)
		v, _ := fKeep.Get(context.Background())
		if v != "keep" {
			t.Fatalf("v=%v", v)
		}
		_ = p.Shutdown(context.Background())
	})

	t.Run("discard_oldest", func(t *testing.T) {
		p, _ := New(Config{Workers: 1, QueueCapacity: 1, Policy: RejectDiscardOldest})
		_, block := blockFirstTask(t, p)
		fOld, _ := p.Submit(Task{ID: "old", Fn: func(context.Context) (any, error) { return "old", nil }})
		fNew, err := p.Submit(Task{ID: "new", Fn: func(context.Context) (any, error) { return "new", nil }})
		if err != nil {
			t.Fatal(err)
		}
		if fOld.Snapshot().State != "canceled" {
			t.Fatalf("oldest state=%s want canceled", fOld.Snapshot().State)
		}
		close(block)
		v, _ := fNew.Get(context.Background())
		if v != "new" {
			t.Fatalf("v=%v", v)
		}
		_ = p.Shutdown(context.Background())
	})

	t.Run("caller_runs", func(t *testing.T) {
		p, _ := New(Config{Workers: 1, QueueCapacity: 1, Policy: RejectCallerRuns})
		_, block := blockFirstTask(t, p)
		_, _ = p.Submit(Task{Fn: func(context.Context) (any, error) { return nil, nil }})
		var ranInline int64
		doneInline := make(chan struct{})
		go func() {
			_, err := p.Submit(Task{Fn: func(context.Context) (any, error) {
				atomic.StoreInt64(&ranInline, 1)
				close(doneInline)
				return nil, nil
			}})
			if err != nil {
				t.Errorf("caller-runs submit: %v", err)
			}
		}()
		select {
		case <-doneInline:
		case <-time.After(500 * time.Millisecond):
		}
		// The overflow task must have been run on the caller goroutine.
		if atomic.LoadInt64(&ranInline) != 1 {
			t.Fatal("overflow task was not run on caller")
		}
		close(block)
		_ = p.Shutdown(context.Background())
	})
}

func TestInterleavedSubmitResizeShrinkShutdown(t *testing.T) {
	rec := NewEventRecorder(4096)
	p, _ := New(Config{Name: "interleave", Workers: 4, QueueCapacity: 500, Sink: rec})

	const producers = 8
	const perProducer = 150
	var total int64 = producers * perProducer
	var done int64
	var wg sync.WaitGroup
	futs := make(chan *Future, total)
	stop := make(chan struct{})

	// Resizer churns worker count continuously.
	go func() {
		sizes := []int{8, 2, 6, 1, 4, 0, 3, 7, 2}
		i := 0
		for {
			select {
			case <-stop:
				return
			default:
			}
			_ = p.Resize(sizes[i%len(sizes)])
			i++
			runtime.Gosched()
		}
	}()

	for pr := 0; pr < producers; pr++ {
		wg.Add(1)
		go func(seed int) {
			defer wg.Done()
			for j := 0; j < perProducer; j++ {
				k := j
				f, err := p.Submit(Task{Fn: func(ctx context.Context) (any, error) {
					// mix quick and briefly blocking tasks
					if (seed+k)%11 == 0 {
						time.Sleep(time.Millisecond)
					}
					atomic.AddInt64(&done, 1)
					return k, nil
				}})
				if err == nil {
					futs <- f
				} else {
					// queue could momentarily fill: count as not-accepted
					atomic.AddInt64(&total, -1)
				}
			}
		}(pr)
	}
	wg.Wait()
	close(stop)

	accepted := len(futs)
	if int64(accepted) != atomic.LoadInt64(&total) {
		t.Fatalf("channel count mismatch: %d vs %d", accepted, total)
	}
	close(futs)

	// Graceful shutdown: every accepted task must complete exactly once.
	if err := p.Shutdown(context.Background()); err != nil {
		t.Fatal(err)
	}
	if got := atomic.LoadInt64(&done); got != total {
		t.Fatalf("done=%d accepted=%d (lost tasks)", got, total)
	}
	st := p.Stats()
	if st.Completed+st.Failed != total {
		t.Fatalf("settled=%d, accepted=%d", st.Completed+st.Failed, total)
	}
	if st.RunningNow != 0 {
		t.Fatalf("running now=%d", st.RunningNow)
	}
	// Cross-check from the futures themselves: exactly one terminal state
	// each, and IDs are all distinct.
	states := map[string]int{}
	seen := map[string]bool{}
	for f := range futs {
		snap := f.Snapshot()
		if seen[snap.ID] {
			t.Fatalf("duplicate future id %s", snap.ID)
		}
		seen[snap.ID] = true
		if !snap.FinishedAt.After(snap.SubmittedAt) || snap.FinishedAt.Equal(snap.SubmittedAt) {
			// equal is allowed for very fast tasks, only zero is wrong
		}
		if snap.State != "completed" && snap.State != "failed" {
			t.Fatalf("task %s in non-terminal state %s after shutdown", snap.ID, snap.State)
		}
		states[snap.State]++
	}
	if int64(states["completed"]+states["failed"]) != total {
		t.Fatalf("future states=%v total=%d", states, total)
	}
	// Every accepted task must have exactly one completed/failed event.
	eventDone := map[string]int{}
	for _, e := range rec.Events() {
		if e.Kind == EventTaskCompleted || e.Kind == EventTaskFailed {
			eventDone[e.TaskID]++
		}
	}
	if len(eventDone) != int(total) {
		t.Fatalf("tasks with terminal events=%d want %d", len(eventDone), total)
	}
	for id, c := range eventDone {
		if c != 1 {
			t.Fatalf("task %s has %d terminal events", id, c)
		}
	}
}

func TestPanicTaskDoesNotKillWorker(t *testing.T) {
	p, _ := New(Config{Workers: 1, QueueCapacity: 10})
	f1, _ := p.Submit(Task{Fn: func(context.Context) (any, error) { panic("boom") }})
	if _, err := f1.Get(context.Background()); !errors.Is(err, ErrTaskPanicked) {
		t.Fatalf("err=%v want ErrTaskPanicked", err)
	}
	f2, _ := p.Submit(Task{Fn: func(context.Context) (any, error) { return "alive", nil }})
	v, err := f2.Get(context.Background())
	if err != nil || v != "alive" {
		t.Fatalf("worker dead after panic: v=%v err=%v", v, err)
	}
	_ = p.Shutdown(context.Background())
}

func TestScheduleWithFakeClock(t *testing.T) {
	clk := NewFakeClock()
	rec := NewEventRecorder(256)
	p, _ := New(Config{Workers: 1, QueueCapacity: 10, Clock: clk, Sink: rec})
	ran := make(chan struct{})
	f, err := p.Schedule(Task{Fn: func(context.Context) (any, error) { close(ran); return nil, nil }}, 5*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if s := f.Snapshot(); s.State != "scheduled" {
		t.Fatalf("state=%s", s.State)
	}
	clk.Advance(4 * time.Second)
	select {
	case <-ran:
		t.Fatal("ran too early")
	case <-time.After(20 * time.Millisecond):
	}
	clk.Advance(2 * time.Second)
	select {
	case <-ran:
	case <-time.After(time.Second):
		t.Fatal("did not run after advancing")
	}
	if _, err := f.Get(context.Background()); err != nil {
		t.Fatal(err)
	}
	_ = p.Shutdown(context.Background())
}

func TestScheduleCapacityCountsScheduled(t *testing.T) {
	clk := NewFakeClock()
	p, _ := New(Config{Workers: 1, QueueCapacity: 2, Clock: clk, Policy: RejectAbort})
	_, err := p.Schedule(Task{Fn: func(context.Context) (any, error) { return nil, nil }}, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	_, err = p.Schedule(Task{Fn: func(context.Context) (any, error) { return nil, nil }}, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	_, err = p.Schedule(Task{Fn: func(context.Context) (any, error) { return nil, nil }}, time.Minute)
	if !errors.Is(err, ErrQueueFull) {
		t.Fatalf("err=%v want ErrQueueFull", err)
	}
	_, _ = p.ShutdownNow(context.Background())
}

func TestEventsOrderAndContent(t *testing.T) {
	rec := NewEventRecorder(1024)
	p, _ := New(Config{Name: "ev", Workers: 1, QueueCapacity: 5, Sink: rec})
	f, _ := p.Submit(Task{ID: "only", Fn: func(context.Context) (any, error) { return 1, nil }})
	_, _ = f.Get(context.Background())
	_ = p.Shutdown(context.Background())

	evs := rec.Events()
	if len(evs) < 6 {
		t.Fatalf("too few events: %d", len(evs))
	}
	if evs[0].Kind != EventPoolCreated {
		t.Fatalf("first event=%s", evs[0].Kind)
	}
	kinds := []EventKind{EventPoolCreated, EventWorkerStarted, EventTaskSubmitted,
		EventTaskStarted, EventTaskCompleted, EventPoolShutdown}
	for _, want := range kinds {
		found := false
		for _, e := range evs {
			if e.Kind == want {
				found = true
			}
		}
		if !found {
			t.Fatalf("missing event %s", want)
		}
	}
	last := evs[len(evs)-1]
	if last.Kind != EventPoolTerminated {
		t.Fatalf("last event=%s want terminated", last.Kind)
	}
}

func TestShutdownTimeoutDoesNotLoseTasks(t *testing.T) {
	p, _ := New(Config{Workers: 1, QueueCapacity: 10})
	release := make(chan struct{})
	f, _ := p.Submit(Task{Fn: func(ctx context.Context) (any, error) { <-release; return nil, nil }})
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	if err := p.Shutdown(ctx); !errors.Is(err, ErrShutdownTimeout) {
		t.Fatalf("err=%v want timeout", err)
	}
	close(release)
	if err := p.AwaitTermination(context.Background()); err != nil {
		t.Fatal(err)
	}
	if _, err := f.Get(context.Background()); err != nil {
		t.Fatalf("task lost after timeout: %v", err)
	}
}

func TestNoGoroutineLeak(t *testing.T) {
	before := runtime.NumGoroutine()
	for iter := 0; iter < 5; iter++ {
		p, _ := New(Config{Workers: 6, QueueCapacity: 200})
		var wg sync.WaitGroup
		for i := 0; i < 120; i++ {
			wg.Add(1)
			i := i
			f, err := p.Submit(Task{Fn: func(ctx context.Context) (any, error) {
				defer wg.Done()
				if i%13 == 0 {
					time.Sleep(2 * time.Millisecond)
				}
				return nil, nil
			}})
			if err != nil {
				wg.Done()
				t.Fatal(err)
			}
			_ = f
		}
		if iter%2 == 0 {
			_ = p.Resize(2)
		}
		if err := p.Shutdown(context.Background()); err != nil {
			t.Fatal(err)
		}
		wg.Wait()
	}
	// Give the scheduler/GC a moment; allow a small slack for test harness
	// goroutines (e.g. the testing package, HTTP clients elsewhere).
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if runtime.NumGoroutine() <= before+2 {
			return
		}
		time.Sleep(50 * time.Millisecond)
		runtime.GC()
	}
	t.Fatalf("goroutine leak: before=%d after=%d", before, runtime.NumGoroutine())
}

// Ensure Stats/Tasks output is coherent under concurrent submissions and
// resizes. Uses bounded work so no goroutine spins on the pool lock.
func TestStatsConcurrent(t *testing.T) {
	p, _ := New(Config{Workers: 4, QueueCapacity: 256, Policy: RejectAbort})
	stop := make(chan struct{})
	var wg sync.WaitGroup

	// Producers submit a bounded number of tasks, then stop.
	for i := 0; i < 4; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < 200; j++ {
				for {
					_, err := p.Submit(Task{Fn: func(context.Context) (any, error) { return nil, nil }})
					if err == nil {
						break
					}
					if errors.Is(err, ErrPoolShuttingDown) {
						return
					}
					time.Sleep(time.Millisecond) // queue full: back off, no spin
				}
			}
		}()
	}
	// Resizer changes the size a bounded number of times.
	wg.Add(1)
	go func() {
		defer wg.Done()
		sizes := []int{8, 1, 3, 6, 2}
		for i := 0; i < 50; i++ {
			select {
			case <-stop:
				return
			default:
			}
			if err := p.Resize(sizes[i%len(sizes)]); err != nil {
				return
			}
			time.Sleep(time.Millisecond)
		}
	}()
	// Reader exercises Stats/Tasks concurrently.
	for i := 0; i < 100; i++ {
		st := p.Stats()
		if st.ActiveWorkers < 0 || st.QueueLen < 0 {
			t.Fatalf("incoherent stats: %+v", st)
		}
		_ = p.Tasks()
		time.Sleep(time.Millisecond)
	}
	close(stop)
	if err := p.Shutdown(context.Background()); err != nil {
		t.Fatal(err)
	}
	wg.Wait()
	if st := p.Stats(); st.Completed+st.Failed != 800 {
		t.Fatalf("settled=%d want 800", st.Completed+st.Failed)
	}
}
