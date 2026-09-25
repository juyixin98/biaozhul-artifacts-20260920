// Package dynpool implements a bounded-queue worker pool whose worker count
// ("thread count") can be adjusted at runtime.
//
// Shrink semantics: shrinking never interrupts running tasks. Workers selected
// to leave finish whatever task they are currently running and exit as soon as
// the queue no longer needs them; accepted tasks are never lost.
//
// Shutdown semantics:
//
//   - Shutdown (graceful): reject new submissions, keep processing all queued
//     tasks to completion, then stop all workers.
//   - ShutdownNow (force): reject new submissions, cancel the context of every
//     running task immediately, drain the wait queue into the returned slice
//     (those tasks never run), and stop all workers.
package dynpool

import (
	"context"
	"errors"
	"sync"
	"sync/atomic"
	"time"
)

// EventType enumerates every structured state transition emitted by a Pool.
type EventType string

const (
	EventPoolCreated    EventType = "pool.created"
	EventPoolShutdown   EventType = "pool.shutdown"      // graceful shutdown initiated
	EventPoolStopped    EventType = "pool.stopped"       // all workers gone (graceful)
	EventPoolForceStop  EventType = "pool.force_stopped" // ShutdownNow completed
	EventWorkerStarted  EventType = "worker.started"
	EventWorkerRetiring EventType = "worker.retiring" // selected to leave; still draining current task
	EventWorkerExited   EventType = "worker.exited"
	EventTaskSubmitted  EventType = "task.submitted" // accepted into the pool
	EventTaskEnqueued   EventType = "task.enqueued"  // queued because no worker was immediately free
	EventTaskStarted    EventType = "task.started"
	EventTaskCompleted  EventType = "task.completed" // exactly once per executed task
	EventTaskRejected   EventType = "task.rejected"
	EventTaskCancelled  EventType = "task.cancelled" // running task observed the force-shutdown context
	EventTaskDropped    EventType = "task.dropped"   // unstarted task discarded by ShutdownNow
	EventResize         EventType = "pool.resized"
)

// Event is one structured state-change record.
type Event struct {
	Seq     int64          `json:"seq"`
	Time    time.Time      `json:"time"`
	Type    EventType      `json:"type"`
	Pool    string         `json:"pool"`
	Worker  int64          `json:"worker,omitempty"`
	TaskID  string         `json:"task_id,omitempty"`
	OldSize int            `json:"old_size,omitempty"`
	NewSize int            `json:"new_size,omitempty"`
	Reason  string         `json:"reason,omitempty"`
	Detail  map[string]any `json:"detail,omitempty"`
}

// Sink receives events. Implementations must be safe for concurrent use.
type Sink interface {
	OnEvent(Event)
}

// SinkFunc adapts a plain function into a Sink.
type SinkFunc func(Event)

func (f SinkFunc) OnEvent(e Event) { f(e) }

// Clock is the replaceable time source used for event timestamps.
type Clock interface {
	Now() time.Time
}

// ClockFunc adapts a function into a Clock.
type ClockFunc func() time.Time

func (f ClockFunc) Now() time.Time { return f() }

// Executor abstracts goroutine launching so that tests can run workers
// synchronously or track every launched goroutine. Execute must run fn
// asynchronously (in a goroutine); it must not invoke fn inline.
type Executor interface {
	Execute(fn func())
}

// ExecutorFunc adapts a function into an Executor.
type ExecutorFunc func(func())

func (f ExecutorFunc) Execute(fn func()) { f(fn) }

// Errors returned by Pool operations.
var (
	ErrPoolShuttingDown = errors.New("dynpool: pool is shutting down")
	ErrPoolStopped      = errors.New("dynpool: pool is stopped")
	ErrInvalidConfig    = errors.New("dynpool: invalid config")
	ErrInvalidSize      = errors.New("dynpool: worker count must be >= 0")
	ErrTaskRejected     = errors.New("dynpool: task rejected")
	ErrDuplicateTaskID  = errors.New("dynpool: duplicate task id")
	ErrEmptyTaskID      = errors.New("dynpool: empty task id")
	ErrNilTask          = errors.New("dynpool: nil task function")
)

// RejectPolicy decides what happens to a submitted task that cannot enter the
// bounded wait queue.
type RejectPolicy string

const (
	// PolicyAbort rejects the submission: Submit returns ErrTaskRejected and a
	// task.rejected event is emitted. The task is never executed.
	PolicyAbort RejectPolicy = "abort"

	// PolicyDiscard silently drops the task (rejected event, no execution).
	PolicyDiscard RejectPolicy = "discard"

	// PolicyDiscardOldest drops the oldest queued task, queues the new one.
	// The dropped task is reported as rejected and never runs.
	PolicyDiscardOldest RejectPolicy = "discard_oldest"

	// PolicyCallerRun executes the task synchronously on the submitting
	// goroutine. It is considered accepted and completed normally; if the pool
	// shuts down while a task is running this way, the task keeps running
	// (Submit cannot be interrupted) and graceful shutdown may wait for it.
	PolicyCallerRun RejectPolicy = "caller_run"
)

// TaskFunc is a unit of work. ctx is cancelled when the pool is force-stopped,
// allowing cooperative blocking tasks to unwind promptly.
type TaskFunc func(ctx context.Context) error

// Config configures a Pool.
type Config struct {
	Name        string        // pool name used in events; default "default"
	Workers     int           // initial worker count
	QueueSize   int           // bounded wait-queue capacity (>= 1 unless CallerRun)
	Reject      RejectPolicy  // behavior when the queue is full; default abort
	Clock       Clock         // event timestamp source; default wall clock
	Sink        Sink          // event sink; default no-op
	Executor    Executor      // goroutine launcher; default real goroutines
	GracePeriod time.Duration // Shutdown drain timeout; 0 = wait forever
}

// State is the lifecycle state of a pool.
type State int

const (
	StateRunning      State = iota
	StateShuttingDown       // graceful Shutdown called
	StateStopping           // ShutdownNow called
	StateStopped
)

func (s State) String() string {
	switch s {
	case StateRunning:
		return "running"
	case StateShuttingDown:
		return "shutting_down"
	case StateStopping:
		return "stopping"
	default:
		return "stopped"
	}
}

// Status is an atomic snapshot of pool state.
type Status struct {
	Name           string `json:"name"`
	State          string `json:"state"`
	TargetWorkers  int    `json:"target_workers"`
	ActiveWorkers  int    `json:"active_workers"`
	Retiring       int    `json:"retiring_workers"`
	Queued         int    `json:"queue_len"`
	QueueCap       int    `json:"queue_cap"`
	RunningTasks   int    `json:"running_tasks"`
	CompletedTasks int64  `json:"completed_tasks"`
	RejectedTasks  int64  `json:"rejected_tasks"`
	CancelledTasks int64  `json:"cancelled_tasks"`
	DroppedTasks   int64  `json:"dropped_tasks"`
}

// Handle tracks a submitted task. All methods are safe for concurrent use.
type Handle struct {
	ID string

	mu      sync.Mutex
	done    bool
	started bool
	err     error
	doneCh  chan struct{}
}

// Done returns a channel closed when the task finished executing or was
// discarded before ever starting.
func (h *Handle) Done() <-chan struct{} { return h.doneCh }

// Result blocks until completion and reports whether the task actually ran and
// what error it returned. ran is false for tasks discarded before start
// (rejection policy or ShutdownNow).
func (h *Handle) Result() (ran bool, err error) {
	<-h.doneCh
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.started, h.err
}

// IsDone reports whether the task has finished or been discarded.
func (h *Handle) IsDone() bool {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.done
}

func (h *Handle) markStarted() {
	h.mu.Lock()
	h.started = true
	h.mu.Unlock()
}

func (h *Handle) markFinished(err error) {
	h.mu.Lock()
	if h.done {
		h.mu.Unlock()
		return
	}
	h.done = true
	h.err = err
	h.mu.Unlock()
	close(h.doneCh)
}

// markDiscarded finishes a handle that never ran (rejection / force drop).
func (h *Handle) markDiscarded() {
	h.mu.Lock()
	if h.done {
		h.mu.Unlock()
		return
	}
	h.done = true
	h.mu.Unlock()
	close(h.doneCh)
}

type task struct {
	id string
	fn TaskFunc
	h  *Handle
}

// Pool is the dynamic worker pool.
type Pool struct {
	clock    Clock
	sink     Sink
	executor Executor
	reject   RejectPolicy
	queue    chan *task
	grace    time.Duration
	name     string

	mu       sync.Mutex
	state    State
	target   int // desired worker count
	workers  map[int64]*workerState
	idSeq    int64
	handles  map[string]*Handle
	seq      int64
	retiring int

	active    atomic.Int32 // currently-live worker goroutines
	running   atomic.Int64 // tasks currently executing
	completed atomic.Int64
	rejectedN atomic.Int64
	canceledN atomic.Int64
	droppedN  atomic.Int64
	forced    atomic.Bool // true once ShutdownNow initiated a force stop

	wg       sync.WaitGroup // worker goroutines
	taskWG   sync.WaitGroup // accepted, not-yet-finished tasks (incl. caller-run)
	stopOnce sync.Once
	forceMu  sync.Mutex // serializes Shutdown / ShutdownNow

	ctx       context.Context
	cancelCtx context.CancelFunc

	stoppedCh chan struct{} // closed when state -> stopped

	dropMu      sync.Mutex // guards droppedList
	droppedList []string   // task ids removed without running during ShutdownNow
}

// New creates a pool and starts Workers initial workers.
func New(cfg Config) (*Pool, error) {
	if cfg.Workers < 0 {
		return nil, ErrInvalidSize
	}
	if cfg.QueueSize < 0 {
		return nil, ErrInvalidConfig
	}
	if cfg.QueueSize == 0 && cfg.Reject != "" && cfg.Reject != PolicyCallerRun {
		return nil, ErrInvalidConfig
	}
	if cfg.QueueSize < 1 {
		cfg.QueueSize = 1
	}
	if cfg.Name == "" {
		cfg.Name = "default"
	}
	if cfg.Reject == "" {
		cfg.Reject = PolicyAbort
	}
	switch cfg.Reject {
	case PolicyAbort, PolicyDiscard, PolicyDiscardOldest, PolicyCallerRun:
	default:
		return nil, ErrInvalidConfig
	}
	if cfg.Clock == nil {
		cfg.Clock = ClockFunc(time.Now)
	}
	if cfg.Sink == nil {
		cfg.Sink = SinkFunc(func(Event) {})
	}
	if cfg.Executor == nil {
		cfg.Executor = defaultExecutor{}
	}

	ctx, cancel := context.WithCancel(context.Background())
	p := &Pool{
		clock:     cfg.Clock,
		sink:      cfg.Sink,
		executor:  cfg.Executor,
		reject:    cfg.Reject,
		queue:     make(chan *task, cfg.QueueSize),
		grace:     cfg.GracePeriod,
		name:      cfg.Name,
		state:     StateRunning,
		target:    cfg.Workers,
		workers:   make(map[int64]*workerState),
		handles:   make(map[string]*Handle),
		ctx:       ctx,
		cancelCtx: cancel,
		stoppedCh: make(chan struct{}),
	}
	p.emit(EventPoolCreated, 0, "", Event{NewSize: cfg.Workers})
	if cfg.Workers > 0 {
		p.mu.Lock()
		spawn := p.collectSpawnLocked(cfg.Workers)
		p.mu.Unlock()
		p.spawnAll(spawn)
	}
	return p, nil
}

// defaultExecutor launches real goroutines.
type defaultExecutor struct{}

func (defaultExecutor) Execute(fn func()) { go fn() }

func (p *Pool) emit(t EventType, worker int64, taskID string, e Event) Event {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.emitLocked(t, worker, taskID, e)
}

// emitLocked stamps and dispatches an event while p.mu is already held.
//
// Sink contract: OnEvent must not call back into the same pool. A
// re-entrant sink would deadlock, same as calling a synchronized collection's
// method from inside its own listener.
func (p *Pool) emitLocked(t EventType, worker int64, taskID string, e Event) Event {
	p.seq++
	e.Seq = p.seq
	e.Type = t
	e.Time = p.clock.Now()
	e.Pool = p.name
	e.Worker = worker
	if taskID != "" {
		e.TaskID = taskID
	}
	p.sink.OnEvent(e)
	return e
}
