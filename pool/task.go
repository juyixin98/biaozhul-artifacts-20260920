package pool

import (
	"context"
	"errors"
	"sync"
	"time"
)

// State is the lifecycle state of a submitted task.
type State int

// Task lifecycle states. State transitions are one-way:
//
//	scheduled -> queued -> running -> completed | failed
//	scheduled | queued -> canceled
//	(rejected and canceled are terminal)
const (
	StateUnknown State = iota
	StateScheduled
	StateQueued
	StateRunning
	StateCompleted
	StateFailed
	StateRejected
	StateCanceled
)

func (s State) String() string {
	switch s {
	case StateScheduled:
		return "scheduled"
	case StateQueued:
		return "queued"
	case StateRunning:
		return "running"
	case StateCompleted:
		return "completed"
	case StateFailed:
		return "failed"
	case StateRejected:
		return "rejected"
	case StateCanceled:
		return "canceled"
	default:
		return "unknown"
	}
}

// IsDone reports whether the state is terminal.
func (s State) IsDone() bool {
	switch s {
	case StateCompleted, StateFailed, StateRejected, StateCanceled:
		return true
	default:
		return false
	}
}

// Task is a unit of work. ID may be empty (one is generated); Type is an
// optional label surfaced on events and snapshots. Fn must be safe to run in
// a worker goroutine. The context passed to Fn is canceled when the pool is
// force-stopped, allowing cooperative interruption.
type Task struct {
	ID   string
	Type string
	Fn   func(ctx context.Context) (any, error)
}

// TaskSnapshot is a point-in-time, race-free view of a Future.
type TaskSnapshot struct {
	ID          string    `json:"id"`
	Type        string    `json:"type,omitempty"`
	State       string    `json:"state"`
	WorkerID    int64     `json:"worker_id,omitempty"`
	SubmittedAt time.Time `json:"submitted_at"`
	StartedAt   time.Time `json:"started_at,omitempty"`
	FinishedAt  time.Time `json:"finished_at,omitempty"`
	Result      any       `json:"result,omitempty"`
	Err         string    `json:"error,omitempty"`
}

// Future represents a task accepted by the pool. Every accepted task settles
// exactly once (completion, failure or cancellation); the same is true for
// rejected submissions.
type Future struct {
	id  string
	typ string

	mu        sync.Mutex
	state     State
	result    any
	err       error
	workerID  int64
	submitted time.Time
	started   time.Time
	finished  time.Time
	done      chan struct{}
	cbs       []func(*Future)
}

func newFuture(id, typ string, now time.Time) *Future {
	return &Future{id: id, typ: typ, state: StateQueued, submitted: now, done: make(chan struct{})}
}

// ID returns the task identifier.
func (f *Future) ID() string { return f.id }

// State returns the current state.
func (f *Future) State() State {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.state
}

// Done is closed once the future reaches a terminal state.
func (f *Future) Done() <-chan struct{} { return f.done }

// Get blocks until the task settles or ctx is done. The returned error is the
// task error on failure, ErrTaskRejected/ErrTaskCanceled for those terminal
// states, or ctx.Err() when the wait is interrupted.
func (f *Future) Get(ctx context.Context) (any, error) {
	select {
	case <-f.done:
	case <-ctx.Done():
		return nil, ctx.Err()
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.result, f.err
}

// Snapshot returns a point-in-time copy of the future's state.
func (f *Future) Snapshot() TaskSnapshot {
	f.mu.Lock()
	defer f.mu.Unlock()
	snap := TaskSnapshot{
		ID:          f.id,
		Type:        f.typ,
		State:       f.state.String(),
		WorkerID:    f.workerID,
		SubmittedAt: f.submitted,
		StartedAt:   f.started,
		FinishedAt:  f.finished,
		Result:      f.result,
	}
	if f.err != nil {
		snap.Err = f.err.Error()
	}
	return snap
}

// OnSettle registers a callback invoked after the future reaches a terminal
// state. If it has already settled the callback runs immediately on the
// caller's goroutine. Callbacks run in pool goroutines and must be quick; a
// panic is recovered.
func (f *Future) OnSettle(cb func(*Future)) {
	f.mu.Lock()
	done := f.state.IsDone()
	if !done {
		f.cbs = append(f.cbs, cb)
	}
	f.mu.Unlock()
	if done {
		safeCallback(f, cb)
	}
}

// transition is called under the pool lock. On reaching a terminal state it
// closes Done and returns callbacks (each already capturing f) that the caller
// must invoke after releasing the lock. A non-terminal transition returns nil.
func (f *Future) transition(state State, result any, err error, now time.Time, workerID int64) []func() {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.state.IsDone() {
		// Exactly-once guard: a task can only settle once. Pool logic hands
		// each work item out once, so this is defensive only.
		return nil
	}
	f.state = state
	f.workerID = workerID
	if state == StateRunning {
		f.started = now
		return nil
	}
	if state.IsDone() {
		f.result = result
		f.err = err
		f.finished = now
		raw := f.cbs
		f.cbs = nil
		close(f.done)
		out := make([]func(), 0, len(raw))
		for _, cb := range raw {
			cb := cb
			out = append(out, func() { cb(f) })
		}
		return out
	}
	return nil
}

// setState moves to a non-terminal state (used for scheduled -> queued).
func (f *Future) setState(s State) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.state = s
}

func safeCallback(f *Future, cb func(*Future)) {
	defer func() { _ = recover() }()
	cb(f)
}

// Executor runs one task body. Providing a custom Executor allows wrapping
// execution (tracing, artificial delay, fault injection). It must be safe for
// concurrent use. Note: the pool also recovers panics around the Executor so
// a buggy implementation cannot kill a worker.
type Executor interface {
	Execute(ctx context.Context, fn func(context.Context) (any, error)) (any, error)
}

// DirectExecutor invokes the task function on the calling worker goroutine.
type DirectExecutor struct{}

// NewDirectExecutor returns the default executor.
func NewDirectExecutor() DirectExecutor { return DirectExecutor{} }

// Execute implements Executor.
func (DirectExecutor) Execute(ctx context.Context, fn func(context.Context) (any, error)) (any, error) {
	return fn(ctx)
}

// Sentinel errors returned by pool operations.
var (
	// ErrQueueFull is returned by the Abort rejection policy.
	ErrQueueFull = errors.New("pool: queue full")
	// ErrTaskDiscarded is returned by the Discard rejection policy.
	ErrTaskDiscarded = errors.New("pool: task discarded")
	// ErrTaskRejected is the error on a future rejected at submission.
	ErrTaskRejected = errors.New("pool: task rejected")
	// ErrTaskCanceled is the error on a future canceled before running.
	ErrTaskCanceled = errors.New("pool: task canceled")
	// ErrTaskPanicked wraps a task function panic.
	ErrTaskPanicked = errors.New("pool: task panicked")
	// ErrPoolShuttingDown is returned by Submit once shutdown has begun.
	ErrPoolShuttingDown = errors.New("pool: shutting down")
	// ErrPoolTerminated is returned by Submit after the pool terminated.
	ErrPoolTerminated = errors.New("pool: terminated")
	// ErrInvalidConfig is returned for bad construction parameters.
	ErrInvalidConfig = errors.New("pool: invalid configuration")
	// ErrShutdownTimeout is returned when a bounded shutdown wait expires.
	ErrShutdownTimeout = errors.New("pool: shutdown wait timed out")
)
