package scheduler

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"time"
)

// BackoffFunc computes the wait before retry attempt n (n >= 1, i.e.
// the wait between attempt n and n+1).
type BackoffFunc func(attempt int) time.Duration

// Option configures an Engine.
type Option func(*Engine)

// WithClock replaces the default wall clock (used for deterministic tests).
func WithClock(c Clock) Option {
	return func(e *Engine) { e.clock = c }
}

// WithSink attaches a structured event sink.
func WithSink(s Sink) Option {
	return func(e *Engine) { e.sink = s }
}

// WithBackoff replaces the default constant 100ms retry backoff.
func WithBackoff(f BackoffFunc) Option {
	return func(e *Engine) { e.backoff = f }
}

// Engine accepts DAG submissions and runs them. It is safe for
// concurrent use; each job runs independently in its own goroutines.
type Engine struct {
	executor Executor
	clock    Clock
	sink     Sink
	backoff  BackoffFunc

	mu    sync.Mutex
	jobs  map[string]*Job
	order []string
	idSeq int64
}

// New creates an Engine bound to the given Executor.
func New(executor Executor, opts ...Option) *Engine {
	e := &Engine{
		executor: executor,
		clock:    RealClock{},
		sink:     SinkFunc(func(Event) {}),
		backoff:  func(int) time.Duration { return 100 * time.Millisecond },
		jobs:     make(map[string]*Job),
	}
	for _, o := range opts {
		o(e)
	}
	return e
}

// Submit validates the spec (including cycle detection) and starts the job.
func (e *Engine) Submit(spec *DagSpec) (*Job, error) {
	if e.executor == nil {
		return nil, errors.New("engine has no executor")
	}
	nodes, dependents, err := validateAndIndex(spec)
	if err != nil {
		return nil, err
	}
	e.mu.Lock()
	e.idSeq++
	id := fmt.Sprintf("job-%d", e.idSeq)
	j := &Job{
		ID:         id,
		engine:     e,
		spec:       *spec,
		nodes:      nodes,
		dependents: dependents,
		states:     make(map[string]*nodeRuntime, len(nodes)),
		reports:    make(chan nodeReport, len(nodes)),
		cancelCh:   make(chan struct{}),
		done:       make(chan struct{}),
		state:      JobRunning,
		createdAt:  e.clock.Now(),
	}
	j.ctx, j.cancel = context.WithCancel(context.Background())
	for name, n := range nodes {
		policy := n.Policy
		if policy == "" {
			policy = RequireAllSuccess
		}
		maxAttempts := n.MaxAttempts
		if maxAttempts <= 0 {
			maxAttempts = 1
		}
		j.states[name] = &nodeRuntime{
			spec:        n,
			policy:      policy,
			maxAttempts: maxAttempts,
			state:       NodePending,
			remaining:   len(n.Deps),
		}
	}
	e.jobs[id] = j
	e.order = append(e.order, id)
	e.mu.Unlock()

	go j.run()
	return j, nil
}

// Get returns a submitted job by ID.
func (e *Engine) Get(id string) (*Job, bool) {
	e.mu.Lock()
	defer e.mu.Unlock()
	j, ok := e.jobs[id]
	return j, ok
}

// Cancel requests cancellation. It returns false when the job is
// unknown or already terminal. Cancellation itself is asynchronous:
// running nodes honor their context; use (*Job).Wait to observe completion.
func (e *Engine) Cancel(id string) bool {
	e.mu.Lock()
	j, ok := e.jobs[id]
	e.mu.Unlock()
	if !ok {
		return false
	}
	j.mu.Lock()
	running := j.state == JobRunning
	j.mu.Unlock()
	if !running {
		return false
	}
	j.cancel()
	return true
}

// List returns all submitted jobs, oldest first.
func (e *Engine) List() []*Job {
	e.mu.Lock()
	defer e.mu.Unlock()
	out := make([]*Job, 0, len(e.order))
	for _, id := range e.order {
		out = append(out, e.jobs[id])
	}
	return out
}
