package scheduler

import (
	"fmt"
	"sync"
	"testing"
	"time"
)

// scriptedExec is a fully controllable Executor:
// per-node behavior functions can block on gates, fail N times, and
// every call is recorded with active/max concurrency counters.
type scriptedExec struct {
	mu      sync.Mutex
	active  map[string]int
	maxSeen map[string]int
	calls   map[string][]callRecord
	total   int

	// funcs maps node name -> behavior. Returning nil means success.
	funcs   map[string]func(ec *ExecContext, attempt int) error
	orderMu sync.Mutex
	started []string
}

type callRecord struct {
	attempt int
}

func newScriptedExec() *scriptedExec {
	return &scriptedExec{
		active:  map[string]int{},
		maxSeen: map[string]int{},
		calls:   map[string][]callRecord{},
		funcs:   map[string]func(*ExecContext, int) error{},
	}
}

func (s *scriptedExec) on(name string, fn func(*ExecContext, int) error) {
	s.funcs[name] = fn
}

// failTimes makes a node fail the first n calls then succeed.
func (s *scriptedExec) failTimes(name string, n int, msg string) {
	s.on(name, func(_ *ExecContext, attempt int) error {
		if attempt <= n {
			return fmt.Errorf("%s on attempt %d", msg, attempt)
		}
		return nil
	})
}

func (s *scriptedExec) alwaysFail(name string) {
	s.on(name, func(_ *ExecContext, attempt int) error {
		return fmt.Errorf("%s always fails (attempt %d)", name, attempt)
	})
}

func (s *scriptedExec) Execute(ec *ExecContext, attempt int) error {
	s.mu.Lock()
	s.active[ec.Node.Name]++
	if s.active[ec.Node.Name] > s.maxSeen[ec.Node.Name] {
		s.maxSeen[ec.Node.Name] = s.active[ec.Node.Name]
	}
	s.calls[ec.Node.Name] = append(s.calls[ec.Node.Name], callRecord{attempt: attempt})
	s.total++
	s.mu.Unlock()

	s.orderMu.Lock()
	s.started = append(s.started, ec.Node.Name)
	s.orderMu.Unlock()

	fn := s.funcs[ec.Node.Name]
	var err error
	if fn != nil {
		err = fn(ec, attempt)
	}

	s.mu.Lock()
	s.active[ec.Node.Name]--
	s.mu.Unlock()
	return err
}

func (s *scriptedExec) callCount(name string) int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.calls[name])
}

func (s *scriptedExec) maxConcurrent(name string) int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.maxSeen[name]
}

func (s *scriptedExec) attemptsOf(name string) []int {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]int, len(s.calls[name]))
	for i, c := range s.calls[name] {
		out[i] = c.attempt
	}
	return out
}

// helper: engine with zero backoff + real clock (fast tests that retry fast)
func newTestEngine(t *testing.T, ex Executor, sink Sink) *Engine {
	t.Helper()
	opts := []Option{WithBackoff(func(int) time.Duration { return time.Millisecond })}
	if sink != nil {
		opts = append(opts, WithSink(sink))
	}
	return New(ex, opts...)
}

func diamondSpec(policy DepPolicy) *DagSpec {
	return &DagSpec{
		Name: "diamond",
		Nodes: []NodeSpec{
			{Name: "A", MaxAttempts: 1},
			{Name: "B", Deps: []string{"A"}, Policy: policy, MaxAttempts: 1},
			{Name: "C", Deps: []string{"A"}, Policy: policy, MaxAttempts: 1},
			{Name: "D", Deps: []string{"B", "C"}, Policy: policy, MaxAttempts: 1},
		},
	}
}

func TestDiamondAllSuccessRunsInOrder(t *testing.T) {
	ex := newScriptedExec()
	sink := NewSliceSink()
	eng := newTestEngine(t, ex, sink)

	job, err := eng.Submit(diamondSpec(RequireAllSuccess))
	if err != nil {
		t.Fatalf("submit: %v", err)
	}
	job.Wait()

	if got := job.State(); got != JobSucceeded {
		t.Fatalf("state = %s, want SUCCEEDED", got)
	}
	for _, n := range []string{"A", "B", "C", "D"} {
		if ex.callCount(n) != 1 {
			t.Errorf("node %s calls = %d, want 1", n, ex.callCount(n))
		}
	}
	// D must run after both B and C.
	bAt, cAt, dAt := indexOf(ex.started, "B"), indexOf(ex.started, "C"), indexOf(ex.started, "D")
	if dAt < bAt || dAt < cAt {
		t.Errorf("D started at %d before B(%d)/C(%d): order=%v", dAt, bAt, cAt, ex.started)
	}
	// A before B and C.
	aAt := indexOf(ex.started, "A")
	if aAt > bAt || aAt > cAt {
		t.Errorf("A started after its dependents: %v", ex.started)
	}
}

func indexOf(slice []string, v string) int {
	for i, s := range slice {
		if s == v {
			return i
		}
	}
	return -1
}
