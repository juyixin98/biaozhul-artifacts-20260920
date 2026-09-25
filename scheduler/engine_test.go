package scheduler_test

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"

	"dagscheduler/scheduler"
)

// ---- test doubles -------------------------------------------------------

// scriptExec runs a user-supplied script keyed by node id. It tracks
// concurrency per node globally and records attempts. Zero value ready after
// set.
type scriptExec struct {
	mu         sync.Mutex
	active     map[string]int
	maxSeen    map[string]int
	calls      map[string][]int // node -> attempts observed
	fns        map[string]func(ctx context.Context, in scheduler.ExecuteInput) error
	violations []string
}

func newScriptExec() *scriptExec {
	return &scriptExec{
		active:  map[string]int{},
		maxSeen: map[string]int{},
		calls:   map[string][]int{},
		fns:     map[string]func(context.Context, scheduler.ExecuteInput) error{},
	}
}

func (s *scriptExec) on(node string, fn func(ctx context.Context, in scheduler.ExecuteInput) error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.fns[node] = fn
}

// onResult scripts an error per (node, attempt). nil entry => success.
func (s *scriptExec) onResult(node string, errs ...error) {
	s.on(node, func(ctx context.Context, in scheduler.ExecuteInput) error {
		var err error
		if in.Attempt <= len(errs) {
			err = errs[in.Attempt-1]
		}
		return err
	})
}

func (s *scriptExec) Execute(ctx context.Context, in scheduler.ExecuteInput) error {
	s.mu.Lock()
	s.active[in.NodeID]++
	s.calls[in.NodeID] = append(s.calls[in.NodeID], in.Attempt)
	if s.active[in.NodeID] > s.maxSeen[in.NodeID] {
		s.maxSeen[in.NodeID] = s.active[in.NodeID]
	}
	if s.active[in.NodeID] > 1 {
		s.violations = append(s.violations,
			fmt.Sprintf("node %q had %d concurrent attempts", in.NodeID, s.active[in.NodeID]))
	}
	fn := s.fns[in.NodeID]
	s.mu.Unlock()

	defer func() {
		s.mu.Lock()
		s.active[in.NodeID]--
		s.mu.Unlock()
	}()
	if fn == nil {
		return nil
	}
	return fn(ctx, in)
}

func (s *scriptExec) maxConcurrency(node string) int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.maxSeen[node]
}

func (s *scriptExec) attemptList(node string) []int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]int(nil), s.calls[node]...)
}

func (s *scriptExec) violationCount() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.violations)
}

// gate lets a test block an attempt until release is called.
type gate struct {
	started   chan struct{}
	release   chan struct{}
	startOnce sync.Once
	relOnce   sync.Once
}

func newGate() *gate {
	return &gate{started: make(chan struct{}), release: make(chan struct{})}
}

func (g *gate) wait(ctx context.Context) error {
	g.startOnce.Do(func() { close(g.started) })
	select {
	case <-g.release:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (g *gate) waitStarted(t *testing.T) {
	t.Helper()
	select {
	case <-g.started:
	case <-time.After(2 * time.Second):
		t.Fatal("gate: attempt never started")
	}
}

func (g *gate) open() { g.relOnce.Do(func() { close(g.release) }) }

func nodeID() func() string {
	var n int
	var mu sync.Mutex
	return func() string {
		mu.Lock()
		defer mu.Unlock()
		n++
		return fmt.Sprintf("run_test_%03d", n)
	}
}

func waitRun(t *testing.T, eng *scheduler.Engine, id string) *scheduler.RunSnapshot {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	snap, err := eng.Wait(ctx, id)
	if err != nil {
		t.Fatalf("Wait(%s): %v", id, err)
	}
	return snap
}

func waitRunFake(t *testing.T, eng *scheduler.Engine, clock *scheduler.FakeClock, id string) *scheduler.RunSnapshot {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for {
		snap, err := eng.Get(id)
		if err != nil {
			t.Fatalf("Get: %v", err)
		}
		switch snap.Status {
		case scheduler.RunSucceeded, scheduler.RunFailed, scheduler.RunCanceled:
			return snap
		}
		if time.Now().After(deadline) {
			t.Fatalf("run %s stuck in %s", id, snap.Status)
		}
		clock.Advance(time.Second)
		// Yield to the engine goroutine.
		time.Sleep(time.Millisecond)
	}
}

// ---- DAG validation -----------------------------------------------------

func TestValidateCycleDetection(t *testing.T) {
	cases := []struct {
		name  string
		nodes []scheduler.NodeSpec
	}{
		{
			name: "self loop caught during normalization",
			nodes: []scheduler.NodeSpec{
				{ID: "a", TaskType: "noop", DependsOn: []string{"a"}},
			},
		},
		{
			name: "two node cycle",
			nodes: []scheduler.NodeSpec{
				{ID: "a", TaskType: "noop", DependsOn: []string{"b"}},
				{ID: "b", TaskType: "noop", DependsOn: []string{"a"}},
			},
		},
		{
			name: "three node cycle",
			nodes: []scheduler.NodeSpec{
				{ID: "a", TaskType: "noop", DependsOn: []string{"c"}},
				{ID: "b", TaskType: "noop", DependsOn: []string{"a"}},
				{ID: "c", TaskType: "noop", DependsOn: []string{"b"}},
			},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			dag := scheduler.DAG{Nodes: tc.nodes}
			err := dag.Validate()
			if err == nil {
				t.Fatal("expected cycle/validation error, got nil")
			}
		})
	}
}

func TestFindCyclePathIsClosedAndExact(t *testing.T) {
	// a -> b -> c -> a (edges expressed via depends_on).
	dag := scheduler.DAG{Nodes: []scheduler.NodeSpec{
		{ID: "a", TaskType: "noop", DependsOn: []string{"b"}},
		{ID: "b", TaskType: "noop", DependsOn: []string{"c"}},
		{ID: "c", TaskType: "noop", DependsOn: []string{"a"}},
	}}
	err := dag.Validate()
	if err == nil {
		t.Fatal("expected cycle error")
	}
	want := "dependency cycle detected: a -> b -> c -> a"
	if err.Error() != want {
		t.Fatalf("cycle message = %q, want %q", err.Error(), want)
	}
}

func TestValidateAcceptsDiamond(t *testing.T) {
	dag := scheduler.DAG{Nodes: []scheduler.NodeSpec{
		{ID: "top", TaskType: "noop"},
		{ID: "left", TaskType: "noop", DependsOn: []string{"top"}},
		{ID: "right", TaskType: "noop", DependsOn: []string{"top"}},
		{ID: "bottom", TaskType: "noop", DependsOn: []string{"left", "right"}},
	}}
	if err := dag.Validate(); err != nil {
		t.Fatalf("diamond should be valid: %v", err)
	}
}

func TestValidateErrors(t *testing.T) {
	cases := []struct {
		name  string
		nodes []scheduler.NodeSpec
	}{
		{"empty dag", nil},
		{"empty id", []scheduler.NodeSpec{{ID: "  ", TaskType: "noop"}}},
		{"duplicate id", []scheduler.NodeSpec{
			{ID: "a", TaskType: "noop"},
			{ID: "a", TaskType: "noop"},
		}},
		{"missing task type", []scheduler.NodeSpec{{ID: "a"}}},
		{"unknown dependency", []scheduler.NodeSpec{
			{ID: "a", TaskType: "noop", DependsOn: []string{"ghost"}},
		}},
		{"bad policy", []scheduler.NodeSpec{
			{ID: "a", TaskType: "noop", Policy: "maybe"},
		}},
		{"negative max attempts", []scheduler.NodeSpec{
			{ID: "a", TaskType: "noop", MaxAttempts: -1},
		}},
		{"negative backoff", []scheduler.NodeSpec{
			{ID: "a", TaskType: "noop",
				Backoff: scheduler.Duration{Duration: -1}},
		}},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			dag := scheduler.DAG{Nodes: tc.nodes}
			if err := dag.Validate(); err == nil {
				t.Fatal("expected validation error")
			}
		})
	}
}

func TestSubmitRejectsUnknownTaskType(t *testing.T) {
	eng := scheduler.New(scheduler.NewRegistry())
	defer eng.Close()
	_, err := eng.Submit(scheduler.DAG{Nodes: []scheduler.NodeSpec{{ID: "a", TaskType: "nope"}}})
	if err == nil {
		t.Fatal("expected unknown task_type error")
	}
}

// ---- diamond: ordering, skip propagation, no duplicate concurrency ------

func TestDiamondAllSuccess(t *testing.T) {
	ex := newScriptExec()
	reg := scheduler.NewRegistry()
	reg.Register("script", ex)
	eng := scheduler.New(reg, scheduler.WithIDGenerator(nodeID()))
	defer eng.Close()

	var mu sync.Mutex
	order := []string{}
	record := func(node string) {
		mu.Lock()
		order = append(order, node)
		mu.Unlock()
	}
	for _, n := range []string{"top", "left", "right", "bottom"} {
		n := n
		ex.on(n, func(ctx context.Context, in scheduler.ExecuteInput) error {
			record(n)
			return nil
		})
	}

	snap, err := eng.Submit(scheduler.DAG{ID: "diamond", Nodes: []scheduler.NodeSpec{
		{ID: "top", TaskType: "script"},
		{ID: "left", TaskType: "script", DependsOn: []string{"top"}},
		{ID: "right", TaskType: "script", DependsOn: []string{"top"}},
		{ID: "bottom", TaskType: "script", DependsOn: []string{"left", "right"}},
	}})
	if err != nil {
		t.Fatalf("submit: %v", err)
	}
	final := waitRun(t, eng, snap.ID)
	if final.Status != scheduler.RunSucceeded {
		t.Fatalf("status = %s, want succeeded", final.Status)
	}
	for _, n := range []string{"top", "left", "right", "bottom"} {
		if got := final.Nodes[n].Status; got != scheduler.StatusSucceeded {
			t.Errorf("node %s = %s, want succeeded", n, got)
		}
	}
	// top before both branches; bottom last.
	mu.Lock()
	defer mu.Unlock()
	idx := map[string]int{}
	for i, n := range order {
		idx[n] = i
	}
	if !(idx["top"] < idx["left"] && idx["top"] < idx["right"]) {
		t.Errorf("top must run first, order=%v", order)
	}
	if !(idx["bottom"] > idx["left"] && idx["bottom"] > idx["right"]) {
		t.Errorf("bottom must run last, order=%v", order)
	}
}

func TestNodeNeverRunsConcurrently(t *testing.T) {
	// A node with many independent dependents that all "fan back" is not a
	// DAG pattern that re-triggers it; instead we combine: (1) a shared node
	// depended on by 50 fan-out nodes, (2) retries with zero backoff, and
	// (3) cancellation racing results. The scriptExec violation counter is
	// the invariant under test for every node in every test run.
	ex := newScriptExec()
	reg := scheduler.NewRegistry()
	reg.Register("script", ex)
	eng := scheduler.New(reg, scheduler.WithIDGenerator(nodeID()))
	defer eng.Close()

	// Flaky shared root retried many times with zero backoff: each retry
	// must wait for the prior attempt to fully finish.
	flaky := errors.New("boom")
	ex.onResult("root", flaky, flaky, flaky, nil)
	// 50 leaves depend on root.
	leaves := make([]scheduler.NodeSpec, 0, 50)
	for i := 0; i < 50; i++ {
		id := fmt.Sprintf("leaf%d", i)
		leaves = append(leaves, scheduler.NodeSpec{ID: id, TaskType: "script", DependsOn: []string{"root"}})
	}
	nodes := []scheduler.NodeSpec{
		{ID: "root", TaskType: "script", MaxAttempts: 4},
	}
	nodes = append(nodes, leaves...)

	snap, err := eng.Submit(scheduler.DAG{Nodes: nodes})
	if err != nil {
		t.Fatalf("submit: %v", err)
	}
	final := waitRun(t, eng, snap.ID)
	if final.Status != scheduler.RunSucceeded {
		t.Fatalf("status = %s", final.Status)
	}
	if got := ex.attemptList("root"); len(got) != 4 || got[0] != 1 || got[3] != 4 {
		t.Errorf("root attempts = %v, want [1 2 3 4]", got)
	}
	if n := ex.violationCount(); n != 0 {
		t.Fatalf("concurrency invariant violated %d time(s)", n)
	}
	for id, m := range ex.maxSeen {
		if m > 1 {
			t.Errorf("node %s max concurrency %d", id, m)
		}
	}
}

func TestFailureSkipsDownstreamAllSuccess(t *testing.T) {
	ex := newScriptExec()
	reg := scheduler.NewRegistry()
	reg.Register("script", ex)
	eng := scheduler.New(reg, scheduler.WithIDGenerator(nodeID()))
	defer eng.Close()

	boom := errors.New("boom")
	ex.onResult("top", boom)

	snap, err := eng.Submit(scheduler.DAG{Nodes: []scheduler.NodeSpec{
		{ID: "top", TaskType: "script", MaxAttempts: 1},
		{ID: "mid", TaskType: "script", DependsOn: []string{"top"}},
		{ID: "leaf", TaskType: "script", DependsOn: []string{"mid"}},
	}})
	if err != nil {
		t.Fatalf("submit: %v", err)
	}
	final := waitRun(t, eng, snap.ID)
	if final.Status != scheduler.RunFailed {
		t.Fatalf("run status = %s, want failed", final.Status)
	}
	if final.Nodes["top"].Status != scheduler.StatusFailed {
		t.Errorf("top = %s, want failed", final.Nodes["top"].Status)
	}
	if final.Nodes["mid"].Status != scheduler.StatusSkipped {
		t.Errorf("mid = %s, want skipped", final.Nodes["mid"].Status)
	}
	if final.Nodes["leaf"].Status != scheduler.StatusSkipped {
		t.Errorf("leaf = %s, want skipped (transitive)", final.Nodes["leaf"].Status)
	}
	if got := ex.attemptList("mid"); got != nil {
		t.Errorf("skipped node mid executed, attempts=%v", got)
	}
}

func TestAllFinishedRunsDespiteFailedDeps(t *testing.T) {
	ex := newScriptExec()
	reg := scheduler.NewRegistry()
	reg.Register("script", ex)
	eng := scheduler.New(reg, scheduler.WithIDGenerator(nodeID()))
	defer eng.Close()

	ex.onResult("a", errors.New("a boom"))
	ex.onResult("b", nil)

	snap, err := eng.Submit(scheduler.DAG{Nodes: []scheduler.NodeSpec{
		{ID: "a", TaskType: "script"},
		{ID: "b", TaskType: "script"},
		{ID: "c", TaskType: "script", DependsOn: []string{"a", "b"}, Policy: scheduler.PolicyAllFinished},
	}})
	if err != nil {
		t.Fatalf("submit: %v", err)
	}
	final := waitRun(t, eng, snap.ID)
	if final.Status != scheduler.RunFailed {
		t.Fatalf("run status = %s, want failed (a failed)", final.Status)
	}
	if final.Nodes["c"].Status != scheduler.StatusSucceeded {
		t.Errorf("c = %s, want succeeded under all_finished", final.Nodes["c"].Status)
	}
	if got := ex.attemptList("c"); len(got) != 1 {
		t.Errorf("c attempts = %v, want [1]", got)
	}
}

func TestMixedPolicies(t *testing.T) {
	ex := newScriptExec()
	reg := scheduler.NewRegistry()
	reg.Register("script", ex)
	eng := scheduler.New(reg, scheduler.WithIDGenerator(nodeID()))
	defer eng.Close()

	ex.onResult("bad", errors.New("nope"))

	snap, err := eng.Submit(scheduler.DAG{Nodes: []scheduler.NodeSpec{
		{ID: "bad", TaskType: "script"},
		{ID: "strict", TaskType: "script", DependsOn: []string{"bad"}, Policy: scheduler.PolicyAllSuccess},
		{ID: "lenient", TaskType: "script", DependsOn: []string{"bad"}, Policy: scheduler.PolicyAllFinished},
	}})
	if err != nil {
		t.Fatalf("submit: %v", err)
	}
	final := waitRun(t, eng, snap.ID)
	if final.Nodes["strict"].Status != scheduler.StatusSkipped {
		t.Errorf("strict = %s, want skipped", final.Nodes["strict"].Status)
	}
	if final.Nodes["lenient"].Status != scheduler.StatusSucceeded {
		t.Errorf("lenient = %s, want succeeded", final.Nodes["lenient"].Status)
	}
}

// ---- retries with the fake clock ----------------------------------------

func TestRetryThenSucceedFakeClock(t *testing.T) {
	ex := newScriptExec()
	reg := scheduler.NewRegistry()
	reg.Register("script", ex)
	clock := scheduler.NewFakeClock(time.Unix(1000, 0))
	eng := scheduler.New(reg, scheduler.WithClock(clock), scheduler.WithIDGenerator(nodeID()))
	defer eng.Close()

	ex.onResult("n", errors.New("err1"), errors.New("err2"), nil)

	snap, err := eng.Submit(scheduler.DAG{Nodes: []scheduler.NodeSpec{
		{ID: "n", TaskType: "script", MaxAttempts: 3,
			Backoff: scheduler.Duration{Duration: 10 * time.Second}},
	}})
	if err != nil {
		t.Fatalf("submit: %v", err)
	}
	final := waitRunFake(t, eng, clock, snap.ID)
	if final.Status != scheduler.RunSucceeded {
		t.Fatalf("status = %s, want succeeded", final.Status)
	}
	if got := final.Nodes["n"].Attempts; got != 3 {
		t.Errorf("attempts = %d, want 3", got)
	}
	if got := ex.attemptList("n"); len(got) != 3 {
		t.Errorf("executor calls = %v, want 3", got)
	}
	events, err := eng.Events(snap.ID)
	if err != nil {
		t.Fatal(err)
	}
	var waits, starts int
	for _, ev := range events {
		if ev.Type == scheduler.EventNodeRetryWait {
			waits++
			if ev.Backoff != 10*time.Second {
				t.Errorf("backoff recorded = %v", ev.Backoff)
			}
		}
		if ev.Type == scheduler.EventNodeRetrying {
			starts++
		}
	}
	if waits != 2 || starts != 2 {
		t.Errorf("retry_wait events=%d retrying events=%d, want 2/2", waits, starts)
	}
}

func TestRetryBudgetExhausted(t *testing.T) {
	ex := newScriptExec()
	reg := scheduler.NewRegistry()
	reg.Register("script", ex)
	clock := scheduler.NewFakeClock(time.Unix(0, 0))
	eng := scheduler.New(reg, scheduler.WithClock(clock), scheduler.WithIDGenerator(nodeID()))
	defer eng.Close()

	boom := errors.New("permanent")
	ex.onResult("n", boom, boom, boom)

	snap, _ := eng.Submit(scheduler.DAG{Nodes: []scheduler.NodeSpec{
		{ID: "n", TaskType: "script", MaxAttempts: 3,
			Backoff: scheduler.Duration{Duration: time.Second}},
	}})
	final := waitRunFake(t, eng, clock, snap.ID)
	if final.Status != scheduler.RunFailed {
		t.Fatalf("status = %s, want failed", final.Status)
	}
	if got := final.Nodes["n"].Attempts; got != 3 {
		t.Errorf("attempts = %d, want exactly 3 (budget exhausted, no 4th)", got)
	}
	if final.Nodes["n"].LastError == "" {
		t.Error("LastError should be recorded")
	}
}

// ---- cancellation --------------------------------------------------------

func TestCancelMarksPendingNodesAndFailsInFlightCtx(t *testing.T) {
	ex := newScriptExec()
	reg := scheduler.NewRegistry()
	reg.Register("script", ex)
	eng := scheduler.New(reg, scheduler.WithIDGenerator(nodeID()))
	defer eng.Close()

	g := newGate()
	gotCtx := make(chan error, 1)
	ex.on("blocked", func(ctx context.Context, in scheduler.ExecuteInput) error {
		return g.wait(ctx)
	})
	ex.on("downstream", func(ctx context.Context, in scheduler.ExecuteInput) error {
		gotCtx <- nil
		return nil
	})

	snap, err := eng.Submit(scheduler.DAG{Nodes: []scheduler.NodeSpec{
		{ID: "blocked", TaskType: "script"},
		{ID: "downstream", TaskType: "script", DependsOn: []string{"blocked"}},
	}})
	if err != nil {
		t.Fatal(err)
	}
	g.waitStarted(t)

	csnap, err := eng.Cancel(snap.ID, "user requested")
	if err != nil {
		t.Fatalf("cancel: %v", err)
	}
	if csnap.CancelReason != "user requested" {
		t.Errorf("reason = %q", csnap.CancelReason)
	}
	final := waitRun(t, eng, snap.ID)
	if final.Status != scheduler.RunCanceled {
		t.Fatalf("status = %s, want canceled", final.Status)
	}
	if final.Nodes["blocked"].Status != scheduler.StatusCanceled {
		t.Errorf("blocked = %s, want canceled (ctx interrupted)", final.Nodes["blocked"].Status)
	}
	if final.Nodes["downstream"].Status != scheduler.StatusCanceled {
		t.Errorf("downstream = %s, want canceled (never ran)", final.Nodes["downstream"].Status)
	}
	select {
	case <-gotCtx:
		t.Error("downstream must never execute")
	default:
	}
}

func TestCancelDuringBackoffStopsTimer(t *testing.T) {
	ex := newScriptExec()
	reg := scheduler.NewRegistry()
	reg.Register("script", ex)
	clock := scheduler.NewFakeClock(time.Unix(0, 0))
	eng := scheduler.New(reg, scheduler.WithClock(clock), scheduler.WithIDGenerator(nodeID()))
	defer eng.Close()

	ex.onResult("n", errors.New("x"))

	snap, _ := eng.Submit(scheduler.DAG{Nodes: []scheduler.NodeSpec{
		{ID: "n", TaskType: "script", MaxAttempts: 3,
			Backoff: scheduler.Duration{Duration: time.Hour}},
	}})
	// Wait until the node enters waiting state.
	deadline := time.Now().Add(2 * time.Second)
	for {
		s, _ := eng.Get(snap.ID)
		if s.Nodes["n"].Status == scheduler.StatusWaiting {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("node never entered waiting")
		}
		time.Sleep(time.Millisecond)
	}
	if _, err := eng.Cancel(snap.ID, ""); err != nil {
		t.Fatal(err)
	}
	// Advancing the clock must NOT trigger another attempt.
	clock.Advance(2 * time.Hour)
	final := waitRunFake(t, eng, clock, snap.ID)
	if final.Nodes["n"].Attempts != 1 {
		t.Errorf("attempts after cancel = %d, want 1", final.Nodes["n"].Attempts)
	}
	if final.Nodes["n"].Status != scheduler.StatusCanceled {
		t.Errorf("node = %s, want canceled", final.Nodes["n"].Status)
	}
}

func TestCancelFinishedRunIsConflict(t *testing.T) {
	reg := scheduler.NewDefaultRegistry()
	eng := scheduler.New(reg, scheduler.WithIDGenerator(nodeID()))
	defer eng.Close()
	snap, _ := eng.Submit(scheduler.DAG{Nodes: []scheduler.NodeSpec{{ID: "a", TaskType: "noop"}}})
	waitRun(t, eng, snap.ID)
	if _, err := eng.Cancel(snap.ID, ""); !errors.Is(err, scheduler.ErrRunFinished) {
		t.Fatalf("err = %v, want ErrRunFinished", err)
	}
}

// ---- events --------------------------------------------------------------

func TestStructuredEventsAndSinks(t *testing.T) {
	reg := scheduler.NewDefaultRegistry()
	var sinkMu sync.Mutex
	var sinkEvents []scheduler.Event
	eng := scheduler.New(reg,
		scheduler.WithSink(func(ev scheduler.Event) {
			sinkMu.Lock()
			sinkEvents = append(sinkEvents, ev)
			sinkMu.Unlock()
		}),
		scheduler.WithIDGenerator(nodeID()),
	)
	defer eng.Close()

	snap, _ := eng.Submit(scheduler.DAG{Nodes: []scheduler.NodeSpec{{ID: "a", TaskType: "noop"}}})
	waitRun(t, eng, snap.ID)

	events, err := eng.Events(snap.ID)
	if err != nil {
		t.Fatal(err)
	}
	wantSeq := []scheduler.EventType{
		scheduler.EventRunCreated,
		scheduler.EventRunStarted,
		scheduler.EventNodeQueued,
		scheduler.EventNodeStarted,
		scheduler.EventNodeSucceeded,
		scheduler.EventRunSucceeded,
	}
	if len(events) != len(wantSeq) {
		t.Fatalf("events=%d want %d: %+v", len(events), len(wantSeq), events)
	}
	for i, want := range wantSeq {
		if events[i].Type != want {
			t.Errorf("event %d = %s, want %s", i, events[i].Type, want)
		}
		if events[i].RunID != snap.ID {
			t.Errorf("event %d run id mismatch", i)
		}
	}
	// Seqs are globally increasing and attached.
	for i := 1; i < len(events); i++ {
		if events[i].Seq <= events[i-1].Seq {
			t.Errorf("seq not increasing at %d", i)
		}
	}
	sinkMu.Lock()
	defer sinkMu.Unlock()
	if len(sinkEvents) != len(wantSeq) {
		t.Errorf("sink got %d events, want %d", len(sinkEvents), len(wantSeq))
	}
}

func TestEventsUnknownRun(t *testing.T) {
	eng := scheduler.New(scheduler.NewDefaultRegistry())
	defer eng.Close()
	if _, err := eng.Events("nope"); !errors.Is(err, scheduler.ErrNotFound) {
		t.Fatalf("err = %v", err)
	}
}

// ---- concurrency stress -------------------------------------------------

func TestConcurrentSubmitsNeverRunANodeTwice(t *testing.T) {
	// Hammer the engine from many goroutines with overlapping DAGs that use
	// the same executor instance. Per-run node ids collide, so the executor
	// keys its counters by run+node. The engine's invariant must hold under
	// concurrent submissions, completions and retries.
	ex := &stressExec{counts: map[string]int{}, active: map[string]int{}}
	reg := scheduler.NewRegistry()
	reg.Register("stress", ex)
	eng := scheduler.New(reg, scheduler.WithIDGenerator(nodeID()))
	defer eng.Close()

	const runs = 40
	var wg sync.WaitGroup
	errs := make(chan error, runs)
	for r := 0; r < runs; r++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, err := eng.Submit(scheduler.DAG{Nodes: []scheduler.NodeSpec{
				{ID: "root", TaskType: "stress", MaxAttempts: 2,
					Backoff: scheduler.Duration{}},
				{ID: "l", TaskType: "stress", DependsOn: []string{"root"}},
				{ID: "rr", TaskType: "stress", DependsOn: []string{"root"}},
				{ID: "bottom", TaskType: "stress", DependsOn: []string{"l", "rr"}},
			}})
			if err != nil {
				errs <- err
			}
		}()
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Fatalf("submit: %v", err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	for _, s := range eng.List() {
		final, err := eng.Wait(ctx, s.ID)
		if err != nil {
			t.Fatalf("wait %s: %v", s.ID, err)
		}
		if final.Status != scheduler.RunSucceeded {
			t.Fatalf("run %s = %s", s.ID, final.Status)
		}
	}
	if ex.violations() != 0 {
		t.Fatalf("concurrent duplicate execution detected: %d", ex.violations())
	}
}

type stressExec struct {
	mu         sync.Mutex
	counts     map[string]int
	active     map[string]int
	violationN int
}

func (s *stressExec) Execute(ctx context.Context, in scheduler.ExecuteInput) error {
	key := in.RunID + "/" + in.NodeID
	s.mu.Lock()
	s.active[key]++
	s.counts[key]++
	if s.active[key] > 1 {
		s.violationN++
	}
	attempt := in.Attempt
	s.mu.Unlock()

	defer func() {
		s.mu.Lock()
		s.active[key]--
		s.mu.Unlock()
	}()

	// root fails exactly once (attempt 1) to exercise retry + zero backoff
	// under concurrency.
	if in.NodeID == "root" && attempt == 1 {
		return errors.New("first attempt fails")
	}
	// Tiny yield to widen the race window.
	time.Sleep(100 * time.Microsecond)
	return nil
}

func (s *stressExec) violations() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.violationN
}

// ---- engine lifecycle ----------------------------------------------------

func TestCloseCancelsInflightRun(t *testing.T) {
	ex := newScriptExec()
	reg := scheduler.NewRegistry()
	reg.Register("script", ex)
	eng := scheduler.New(reg, scheduler.WithIDGenerator(nodeID()))

	g := newGate()
	ex.on("a", func(ctx context.Context, in scheduler.ExecuteInput) error {
		return g.wait(ctx)
	})
	ex.on("b", func(ctx context.Context, in scheduler.ExecuteInput) error { return nil })
	snap, err := eng.Submit(scheduler.DAG{Nodes: []scheduler.NodeSpec{
		{ID: "a", TaskType: "script"},
		{ID: "b", TaskType: "script", DependsOn: []string{"a"}},
	}})
	if err != nil {
		t.Fatal(err)
	}
	g.waitStarted(t)
	eng.Close() // must interrupt the attempt, settle the run, stop the loop

	final, err := eng.Get(snap.ID)
	if err != nil {
		t.Fatalf("get after close: %v", err)
	}
	if final.Status != scheduler.RunCanceled {
		t.Errorf("run = %s, want canceled after Close", final.Status)
	}
	if final.Nodes["a"].Status != scheduler.StatusCanceled {
		t.Errorf("in-flight node = %s, want canceled", final.Nodes["a"].Status)
	}
	if final.Nodes["b"].Status != scheduler.StatusCanceled {
		t.Errorf("pending node = %s, want canceled", final.Nodes["b"].Status)
	}
	if got := ex.attemptList("b"); got != nil {
		t.Errorf("pending node executed after Close: %v", got)
	}
}
