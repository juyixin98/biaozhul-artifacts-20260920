package budget

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	"tokenbudget/internal/clock"
	"tokenbudget/internal/rational"
)

// recordingExecutor is a replaceable executor that records admitted jobs
// instead of running real work.
type recordingExecutor struct {
	mu    sync.Mutex
	calls []string
	fail  bool
}

func (r *recordingExecutor) Execute(_ context.Context, job Job) error {
	// Record by executing a marker: the job itself is what updates state;
	// here we just invoke it (it is a no-op in tests) and note admission.
	_ = job(context.Background())
	r.mu.Lock()
	r.calls = append(r.calls, "called")
	r.mu.Unlock()
	if r.fail {
		return errors.New("executor injected failure")
	}
	return nil
}

func (r *recordingExecutor) len() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.calls)
}

func TestSchedulerUsesReplacedExecutor(t *testing.T) {
	v := clock.NewVirtual(time.Unix(0, 0))
	cfg := Config{
		Global:  BucketConfig{Rate: rational.PerSecond(1), BurstMicro: 1 * MicroPerToken},
		Default: BucketConfig{Rate: rational.PerSecond(1), BurstMicro: 1 * MicroPerToken},
	}
	sink := NewMemorySink(0)
	l := testLimiter(t, v, sink, cfg)
	ex := &recordingExecutor{}
	s := NewScheduler(l, ex, sink)

	noop := func(context.Context) error { return nil }

	r := s.Submit(context.Background(), "a", 1*MicroPerToken, noop)
	if !r.Decision.Allowed {
		t.Fatal("first submit should be admitted")
	}
	if r.Err != "" || ex.len() != 1 {
		t.Fatalf("executor not invoked once: receipt=%+v calls=%d", r, ex.len())
	}

	// The scheduler blocks (waits for refill) when out of budget, so submit
	// the next job concurrently; it must not run until virtual time advances.
	got := make(chan Receipt, 1)
	go func() { got <- s.Submit(context.Background(), "a", 1*MicroPerToken, noop) }()
	v.WaitForWaiters(1)
	if ex.len() != 1 {
		t.Fatal("executor ran before refill (phantom admission)")
	}
	v.Advance(time.Second)
	select {
	case r2 := <-got:
		if !r2.Decision.Allowed || ex.len() != 2 {
			t.Fatalf("post-refill submit: allowed=%v calls=%d", r2.Decision.Allowed, ex.len())
		}
	case <-time.After(2 * time.Second):
		t.Fatal("blocked submit not completed after refill")
	}

	// Executor failures surface in the receipt and an execute event is still
	// recorded with the error detail.
	ex.fail = true
	got2 := make(chan Receipt, 1)
	go func() { got2 <- s.Submit(context.Background(), "a", 1*MicroPerToken, noop) }()
	v.WaitForWaiters(1)
	v.Advance(time.Second)
	r = <-got2
	if !r.Decision.Allowed || r.Err == "" {
		t.Fatalf("executor failure not reported: %+v", r)
	}
	evs := sink.Events("", 0)
	var sawExec bool
	for _, e := range evs {
		if e.Type == EventExecute && e.Detail != "" {
			sawExec = true
		}
	}
	if !sawExec {
		t.Fatal("execute event with error detail missing")
	}
}
