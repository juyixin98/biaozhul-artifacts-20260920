package dynpool

import (
	"context"
	"errors"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func TestRejectDiscard(t *testing.T) {
	p, _ := New(Config{Workers: 1, QueueSize: 1, Reject: PolicyDiscard})
	b := newBlockTask("b")
	if _, err := p.Submit(b.fn); err != nil {
		t.Fatal(err)
	}
	<-b.started
	if _, err := p.Submit(func(context.Context) error { return nil }); err != nil {
		t.Fatal(err) // occupies the single queue slot
	}
	waitFor(t, time.Second, func() bool { return p.Status().Queued == 1 }, "queue slot taken")
	var ran atomic.Int64
	h, err := p.Submit(func(context.Context) error { ran.Add(1); return nil })
	if err != nil {
		t.Fatalf("discard policy returns nil error, got %v", err)
	}
	if ranRes, _ := h.Result(); ranRes {
		t.Fatal("discarded task must not run")
	}
	if ran.Load() != 0 {
		t.Fatal("discarded task ran")
	}
	if p.Status().RejectedTasks != 1 {
		t.Fatalf("rejected=%d want 1", p.Status().RejectedTasks)
	}
	close(b.release)
	if err := p.Shutdown(context.Background()); err != nil {
		t.Fatal(err)
	}
}

func TestRejectDiscardOldest(t *testing.T) {
	p, _ := New(Config{Workers: 1, QueueSize: 2, Reject: PolicyDiscardOldest})
	b := newBlockTask("b")
	if _, err := p.Submit(b.fn); err != nil {
		t.Fatal(err)
	}
	<-b.started

	var order []int32
	var mu sync.Mutex
	submitOrdered := func(v int32) *Handle {
		h, err := p.Submit(func(context.Context) error {
			mu.Lock()
			order = append(order, v)
			mu.Unlock()
			return nil
		})
		if err != nil {
			t.Fatal(err)
		}
		return h
	}
	hOldest := submitOrdered(1)
	_ = submitOrdered(2)
	// Queue full now [1, 2]; submitting 3 evicts 1.
	_ = submitOrdered(3)
	if ran, _ := hOldest.Result(); ran {
		t.Fatal("evicted oldest task must not run")
	}
	close(b.release)
	if err := p.Shutdown(context.Background()); err != nil {
		t.Fatal(err)
	}
	if len(order) != 2 || order[0] != 2 || order[1] != 3 {
		t.Fatalf("executed order=%v, want [2 3]", order)
	}
	if p.Status().RejectedTasks != 1 {
		t.Fatalf("rejected=%d want 1", p.Status().RejectedTasks)
	}
}

func TestRejectCallerRun(t *testing.T) {
	exec := &TrackedExecutor{}
	p, _ := New(Config{Workers: 1, QueueSize: 1, Reject: PolicyCallerRun, Executor: exec})
	b := newBlockTask("b")
	if _, err := p.Submit(b.fn); err != nil {
		t.Fatal(err)
	}
	<-b.started
	if _, err := p.Submit(func(context.Context) error { return nil }); err != nil { // queue slot
		t.Fatal(err)
	}
	waitFor(t, time.Second, func() bool { return p.Status().Queued == 1 }, "queue slot taken")

	var callerRan atomic.Bool
	done := make(chan struct{})
	h, err := p.Submit(func(ctx context.Context) error {
		// Runs on this test goroutine synchronously.
		callerRan.Store(true)
		close(done)
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	select {
	case <-done:
	default:
		t.Fatal("caller_run must execute synchronously before Submit returns")
	}
	if !callerRan.Load() {
		t.Fatal("caller_run did not run on caller")
	}
	if ran, err := h.Result(); !ran || err != nil {
		t.Fatalf("caller-run result ran=%v err=%v", ran, err)
	}
	if p.Status().RejectedTasks != 0 {
		t.Fatalf("caller_run is not a rejection, rejected=%d", p.Status().RejectedTasks)
	}
	close(b.release)
	if err := p.Shutdown(context.Background()); err != nil {
		t.Fatal(err)
	}
	if !exec.WaitActive(time.Second) {
		t.Fatal("goroutine leak")
	}
}

func TestDuplicateAndInvalidIDs(t *testing.T) {
	p, _ := New(Config{Workers: 1, QueueSize: 4})
	noop := func(context.Context) error { return nil }
	if _, err := p.Submit(noop, WithTaskID("x")); err != nil {
		t.Fatal(err)
	}
	if _, err := p.Submit(noop, WithTaskID("x")); !errors.Is(err, ErrDuplicateTaskID) {
		t.Fatalf("dup id: %v", err)
	}
	if _, err := p.Submit(nil); !errors.Is(err, ErrNilTask) {
		t.Fatalf("nil task: %v", err)
	}
	if _, err := New(Config{Workers: -1}); !errors.Is(err, ErrInvalidSize) {
		t.Fatalf("bad workers: %v", err)
	}
	if _, err := New(Config{Workers: 1, QueueSize: 0, Reject: PolicyAbort}); !errors.Is(err, ErrInvalidConfig) {
		t.Fatalf("q=0 without caller_run should be invalid: %v", err)
	}
	if _, err := New(Config{Workers: 1, Reject: "bogus"}); !errors.Is(err, ErrInvalidConfig) {
		t.Fatalf("bogus policy: %v", err)
	}
	if err := p.Shutdown(context.Background()); err != nil {
		t.Fatal(err)
	}
}

func TestEventOrderingAndContent(t *testing.T) {
	sink := &MemorySink{}
	p, _ := New(Config{Name: "ev", Workers: 1, QueueSize: 4, Sink: sink})
	if _, err := p.Submit(func(context.Context) error { return nil }, WithTaskID("hello")); err != nil {
		t.Fatal(err)
	}
	if err := p.Shutdown(context.Background()); err != nil {
		t.Fatal(err)
	}
	evs := sink.Events()
	if evs[0].Type != EventPoolCreated {
		t.Fatalf("first=%s", evs[0].Type)
	}
	if evs[0].Pool != "ev" {
		t.Fatalf("pool name=%q", evs[0].Pool)
	}
	var seq int64
	want := map[EventType]bool{
		EventPoolCreated: true, EventWorkerStarted: true,
		EventTaskSubmitted: true, EventTaskEnqueued: true,
		EventTaskStarted: true, EventTaskCompleted: true,
		EventPoolShutdown: true, EventWorkerRetiring: true,
		EventWorkerExited: true, EventPoolStopped: true,
	}
	got := map[EventType]bool{}
	for _, e := range evs {
		if e.Seq <= seq {
			t.Fatalf("non-monotonic seq: %d after %d", e.Seq, seq)
		}
		seq = e.Seq
		if e.Time.IsZero() {
			t.Fatal("zero timestamp")
		}
		got[e.Type] = true
	}
	for ty := range want {
		if !got[ty] {
			t.Fatalf("missing event %s; got %v", ty, got)
		}
	}
	// enqueued/submitted/started/completed carry the task id.
	for _, e := range evs {
		if e.Type == EventTaskStarted {
			if e.TaskID != "hello" {
				t.Fatalf("task id=%q", e.TaskID)
			}
		}
	}
}

func TestMultiAndJSONSink(t *testing.T) {
	var a, b MemorySink
	p, _ := New(Config{Workers: 1, QueueSize: 2, Sink: MultiSink(&a, &b)})
	if _, err := p.Submit(func(context.Context) error { return nil }); err != nil {
		t.Fatal(err)
	}
	if err := p.Shutdown(context.Background()); err != nil {
		t.Fatal(err)
	}
	if a.Len() != b.Len() || a.Len() == 0 {
		t.Fatalf("multi fan-out mismatch a=%d b=%d", a.Len(), b.Len())
	}
}

func TestReplaceableClock(t *testing.T) {
	now := time.Date(2020, 1, 2, 3, 4, 5, 0, time.UTC)
	var ticks atomic.Int64
	clock := ClockFunc(func() time.Time {
		n := ticks.Add(1)
		return now.Add(time.Duration(n) * time.Second)
	})
	sink := &MemorySink{}
	p, _ := New(Config{Workers: 1, QueueSize: 1, Clock: clock, Sink: sink})
	if err := p.Shutdown(context.Background()); err != nil {
		t.Fatal(err)
	}
	first := sink.Events()[0].Time
	if !first.Equal(now.Add(time.Second)) {
		t.Fatalf("fake clock not used: %v", first)
	}
}

func TestStatusSnapshot(t *testing.T) {
	p, _ := New(Config{Name: "s", Workers: 2, QueueSize: 5})
	waitFor(t, time.Second, func() bool { return p.Status().ActiveWorkers == 2 }, "workers up")
	s := p.Status()
	if s.Name != "s" || s.State != "running" || s.TargetWorkers != 2 ||
		s.ActiveWorkers != 2 || s.QueueCap != 5 {
		t.Fatalf("bad snapshot: %+v", s)
	}
	if err := p.Resize(4); err != nil {
		t.Fatal(err)
	}
	waitFor(t, time.Second, func() bool { return p.Status().ActiveWorkers == 4 }, "4 up")
	if err := p.Shutdown(context.Background()); err != nil {
		t.Fatal(err)
	}
}
