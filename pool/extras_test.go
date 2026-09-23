package pool

import (
	"bytes"
	"context"
	"strings"
	"testing"
	"time"
)

func TestFutureAccessorsAndDone(t *testing.T) {
	p, _ := New(Config{Workers: 1, QueueCapacity: 4})

	f, err := p.Submit(Task{ID: "acc", Type: "t", Fn: func(context.Context) (any, error) {
		return 7, nil
	}})
	if err != nil {
		t.Fatal(err)
	}
	if f.ID() != "acc" {
		t.Fatalf("id=%s", f.ID())
	}
	<-f.Done()
	if f.State() != StateCompleted {
		t.Fatalf("state=%s", f.State())
	}
	v, err := f.Get(context.Background())
	if err != nil || v.(int) != 7 {
		t.Fatalf("v=%v err=%v", v, err)
	}

	// A not-yet-done future waited on with a canceled context returns the
	// context error. Use a task that exits quickly so ShutdownNow can wind
	// the pool down without waiting on ctx.
	pending, _ := p.Submit(Task{Fn: func(ctx context.Context) (any, error) {
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(30 * time.Second):
			return nil, nil
		}
	}})
	cctx, ccancel := context.WithCancel(context.Background())
	ccancel()
	if _, err := pending.Get(cctx); err == nil {
		t.Fatal("expected context error waiting on pending future")
	}
	if _, err := p.ShutdownNow(context.Background()); err != nil {
		t.Fatal(err)
	}
}

func TestEventRecorderSubscribe(t *testing.T) {
	rec := NewEventRecorder(32)
	p, _ := New(Config{Name: "sub", Workers: 1, QueueCapacity: 4, Sink: rec})
	// Subscribe with enough buffer to hold the whole lifecycle.
	ch, unsub := rec.Subscribe(32)
	f, _ := p.Submit(Task{Fn: func(context.Context) (any, error) { return nil, nil }})
	<-f.Done()
	p.Shutdown(context.Background())

	var kinds []EventKind
drain:
	for {
		select {
		case e := <-ch:
			kinds = append(kinds, e.Kind)
		default:
			break drain
		}
	}
	found := map[EventKind]bool{}
	for _, k := range kinds {
		found[k] = true
	}
	if !found[EventTaskSubmitted] || !found[EventPoolTerminated] {
		t.Fatalf("subscribed events=%v", kinds)
	}
	unsub()

	// Ring retains events in order.
	evs := rec.Events()
	if evs[0].Kind != EventPoolCreated {
		t.Fatalf("first=%s", evs[0].Kind)
	}
	if len(evs) == 0 || evs[len(evs)-1].Kind != EventPoolTerminated {
		t.Fatal("missing terminated")
	}
}

func TestJSONAndMultiSink(t *testing.T) {
	var buf bytes.Buffer
	js := NewJSONSink(&buf)
	rec := NewEventRecorder(32)
	sink := MultiSink(js, rec)
	p, _ := New(Config{Name: "json", Workers: 1, QueueCapacity: 2, Sink: sink})
	f, _ := p.Submit(Task{ID: "j", Fn: func(context.Context) (any, error) { return nil, nil }})
	<-f.Done()
	p.Shutdown(context.Background())

	lines := strings.Split(strings.TrimSpace(buf.String()), "\n")
	if len(lines) < 3 {
		t.Fatalf("ndjson lines=%d", len(lines))
	}
	for _, ln := range lines {
		if !strings.Contains(ln, `"pool":"json"`) {
			t.Fatalf("line missing pool: %s", ln)
		}
	}
	if len(rec.Events()) != len(lines) {
		t.Fatalf("recorder=%d json=%d", len(rec.Events()), len(lines))
	}
}

func TestFakeClockTimerStopReset(t *testing.T) {
	clk := NewFakeClockAt(time.Unix(100, 0))
	if clk.Now().Unix() != 100 {
		t.Fatalf("now=%v", clk.Now())
	}
	tm := clk.NewTimer(time.Minute)
	if clk.PendingTimers() != 1 {
		t.Fatalf("pending=%d", clk.PendingTimers())
	}
	clk.Advance(30 * time.Second)
	select {
	case <-tm.C():
		t.Fatal("fired early")
	default:
	}
	if !tm.Stop() {
		t.Fatal("stop should return true")
	}
	clk.Advance(time.Minute)
	select {
	case <-tm.C():
		t.Fatal("stopped timer fired")
	default:
	}
	// Reset re-arms.
	if tm.Reset(time.Minute) {
		// existed (stopped) -> boolean is implementation detail; accept either
	}
	clk.Advance(time.Minute)
	select {
	case <-tm.C():
	default:
		t.Fatal("reset timer did not fire")
	}
}

func TestStateString(t *testing.T) {
	cases := map[State]string{
		StateScheduled: "scheduled", StateQueued: "queued", StateRunning: "running",
		StateCompleted: "completed", StateFailed: "failed",
		StateRejected: "rejected", StateCanceled: "canceled", StateUnknown: "unknown",
	}
	for s, want := range cases {
		if s.String() != want {
			t.Fatalf("%d=%s want %s", s, s.String(), want)
		}
	}
	if StateCompleted.IsDone() != true || StateRunning.IsDone() != false {
		t.Fatal("IsDone wrong")
	}
}

func TestRejectPolicyString(t *testing.T) {
	for p, want := range map[RejectPolicy]string{
		RejectAbort: "abort", RejectCallerRuns: "caller_runs",
		RejectDiscard: "discard", RejectDiscardOldest: "discard_oldest",
	} {
		if p.String() != want {
			t.Fatalf("%d=%s", p, p.String())
		}
	}
}

func TestNameAndInvalidConfig(t *testing.T) {
	if _, err := New(Config{QueueCapacity: 0}); err == nil {
		t.Fatal("expected error for queue 0")
	}
	if _, err := New(Config{QueueCapacity: 1, Workers: -1}); err == nil {
		t.Fatal("expected error for negative workers")
	}
	if _, err := New(Config{QueueCapacity: 1, Workers: MaxWorkers + 1}); err == nil {
		t.Fatal("expected error for too many workers")
	}
	p, err := New(Config{QueueCapacity: 1})
	if err != nil {
		t.Fatal(err)
	}
	if p.Name() != "pool" {
		t.Fatalf("default name=%s", p.Name())
	}
	p.Shutdown(context.Background())
}

func TestSubmitNilTask(t *testing.T) {
	p, _ := New(Config{Workers: 1, QueueCapacity: 1})
	defer p.Shutdown(context.Background())
	if _, err := p.Submit(Task{}); err == nil {
		t.Fatal("nil Fn should error")
	}
	if _, err := p.Schedule(Task{}, time.Second); err == nil {
		t.Fatal("nil Fn schedule should error")
	}
}

// A callback registered after settle fires immediately without blocking.
func TestOnSettleAfterDone(t *testing.T) {
	p, _ := New(Config{Workers: 1, QueueCapacity: 1})
	defer p.Shutdown(context.Background())
	f, _ := p.Submit(Task{Fn: func(context.Context) (any, error) { return nil, nil }})
	<-f.Done()
	ran := make(chan struct{})
	f.OnSettle(func(*Future) { close(ran) })
	select {
	case <-ran:
	case <-time.After(time.Second):
		t.Fatal("late OnSettle did not fire")
	}
}
