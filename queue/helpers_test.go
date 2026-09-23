package queue_test

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"

	"agingqueue/clock"
	"agingqueue/queue"
)

// blockExec executes jobs whose type is "block" by blocking until the job's
// release channel is closed or its context is canceled. Other types can be
// wired via Extra.
type blockExec struct {
	mu       sync.Mutex
	started  []string
	releases map[string]chan struct{}
	extra    map[string]queue.Executor
}

func newBlockExec() *blockExec {
	return &blockExec{
		releases: make(map[string]chan struct{}),
		extra:    make(map[string]queue.Executor),
	}
}

func (b *blockExec) Execute(ctx context.Context, j *queue.Job) error {
	b.mu.Lock()
	b.started = append(b.started, j.ID)
	ch := b.releases[j.ID]
	extra := b.extra[j.Type]
	b.mu.Unlock()

	if extra != nil {
		return extra.Execute(ctx, j)
	}
	if j.Type != "block" {
		return fmt.Errorf("blockExec: unknown type %q", j.Type)
	}
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-ch:
		return nil
	}
}

func (b *blockExec) startedList() []string {
	b.mu.Lock()
	defer b.mu.Unlock()
	out := make([]string, len(b.started))
	copy(out, b.started)
	return out
}

// setExtra registers a type-specific executor (under the same lock Execute
// uses, so it is race-free even while the scheduler runs).
func (b *blockExec) setExtra(typ string, e queue.Executor) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.extra[typ] = e
}

// setRelease registers a job's release channel.
func (b *blockExec) setRelease(id string, ch chan struct{}) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.releases[id] = ch
}

func (b *blockExec) release(id string) chan struct{} {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.releases[id]
}

// failNTimes fails the first n attempts of every job it sees, then succeeds.
type failNTimes struct{ n int }

func (f failNTimes) Execute(_ context.Context, j *queue.Job) error {
	if j.Attempts <= f.n {
		return fmt.Errorf("failNTimes: attempt %d", j.Attempts)
	}
	return nil
}

// alwaysFail fails forever (jobs end up Failed via MaxAttempts).
type alwaysFail struct{}

func (alwaysFail) Execute(_ context.Context, _ *queue.Job) error {
	return errors.New("boom")
}

type fatalExec struct{}

func (fatalExec) Execute(_ context.Context, _ *queue.Job) error {
	return queue.Fatal(errors.New("cannot proceed"))
}

type testHarness struct {
	clk  *clock.Fake
	exec *blockExec
	sink *queue.MemorySink
	s    *queue.Scheduler
}

func newHarness(t *testing.T, cfg queue.Config) *testHarness {
	t.Helper()
	clk := clock.NewFake(time.Unix(1_000_000, 0))
	exec := newBlockExec()
	sink := queue.NewMemorySink(0)
	cfg.Clock = clk
	cfg.Executor = exec
	cfg.Sink = sink
	s, err := queue.New(cfg)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	t.Cleanup(func() { _ = s.Close() })
	return &testHarness{clk: clk, exec: exec, sink: sink, s: s}
}

// wait polls in REAL time (the fake clock never moves by itself) until
// cond is true or the deadline expires.
func wait(t *testing.T, timeout time.Duration, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(time.Millisecond)
	}
	if !cond() {
		t.Fatalf("condition not met within %s", timeout)
	}
}

func (h *testHarness) waitStatus(t *testing.T, id string, want queue.Status) {
	t.Helper()
	wait(t, 2*time.Second, func() bool {
		j, err := h.s.Get(id)
		return err == nil && j.Status == want
	})
}

func (h *testHarness) eventTypes(jobID string) []queue.EventType {
	var out []queue.EventType
	for _, e := range h.sink.Events() {
		if e.JobID == jobID {
			out = append(out, e.Type)
		}
	}
	return out
}

func (h *testHarness) mustSubmit(t *testing.T, req queue.Submit) string {
	t.Helper()
	j, err := h.s.Submit(req)
	if err != nil {
		t.Fatalf("Submit(%+v): %v", req, err)
	}
	return j.ID
}

func (h *testHarness) get(t *testing.T, id string) *queue.Job {
	t.Helper()
	j, err := h.s.Get(id)
	if err != nil {
		t.Fatalf("Get(%s): %v", id, err)
	}
	return j
}

// advance jumps the fake clock by d and deterministically waits until the
// scheduler has processed every consequence of the jump (due promotions,
// delay releases, dispatches, and instant-attempt cascades).
//
// It relies on two properties of the clock/scheduler contract:
//  1. ArmTimer schedules an ABSOLUTE deadline atomically relative to jumps
//     (stop-old/arm-new/evaluate-due under the clock lock), so a jump can
//     never land in a window that loses a deadline.
//  2. After the jump, WaitSettledAt proves all due work was processed; a
//     jump that fires no timer leaves the loop parked on a still-future
//     timer, which the settle check recognizes as "nothing due".
func (h *testHarness) advance(d time.Duration) {
	target := h.clk.Now().Add(d)
	h.clk.Advance(d)
	if !h.s.WaitSettledAt(target, 2*time.Second) {
		panic("scheduler did not settle after clock jump")
	}
}

// waitEventCount polls until the job has at least n events of the given
// type, or fails the test.
func (h *testHarness) waitEventCount(t *testing.T, jobID string, typ queue.EventType, n int) {
	t.Helper()
	wait(t, 2*time.Second, func() bool {
		return countEvents(h.sink, typ, jobID) >= n
	})
}
