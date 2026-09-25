package scheduler

import (
	"context"
	"errors"
	"fmt"
	"math/rand"
	"runtime"
	"runtime/debug"
	"sync"
	"time"
)

// isCancellationError reports whether err represents context
// cancellation (deadline or explicit cancel), possibly wrapped.
func isCancellationError(err error) bool {
	return errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded)
}

// Func is the unit of executable work. It receives a task context
// (canceled when the task is canceled or when forceful shutdown
// cancels its root) and a Runtime for spawning and waiting.
type Func func(ctx context.Context, rt Runtime) (any, error)

// TaskFunc builds a task function from a free-form JSON-decoded
// payload. Registered kinds make tasks submit-table through the HTTP
// API.
type TaskFunc func(ctx context.Context, rt Runtime, payload any) (any, error)

// Runtime is the scheduling API visible to running tasks.
type Runtime interface {
	// WorkerID returns the 1-based id of the worker executing the
	// current task.
	WorkerID() int
	// Now returns the executor clock time.
	Now() time.Time
	// Spawn creates a child task of a registered kind. The child is
	// pushed onto the current worker's local deque.
	Spawn(ctx context.Context, kind string, payload any) (Handle, error)
	// SpawnFunc creates a child task from an inline function (library
	// use; not reachable through HTTP).
	SpawnFunc(ctx context.Context, name string, fn Func) (Handle, error)
	// Wait blocks until h reaches a terminal state, the context is
	// canceled, or the executor shuts down. While blocked, the current
	// worker keeps executing other tasks (including the waited one),
	// which prevents thread-pool starvation deadlocks. The result error
	// is the target task's error; h.State()/Snapshot distinguishes
	// failed, panicked and canceled outcomes.
	Wait(ctx context.Context, h Handle) (any, error)
	// Sleep is a clock-aware sleep: with a MockClock it completes when
	// the clock is advanced past d. Returns ctx.Err() on cancellation.
	Sleep(ctx context.Context, d time.Duration) error
}

// Handle identifies a submitted or spawned task.
type Handle interface {
	// ID is the unique, stable task identifier.
	ID() string
	// Done is closed once the task reaches a terminal state.
	Done() <-chan struct{}
	// Snapshot returns the current state of the task.
	Snapshot() TaskInfo
}

// TaskInfo is an immutable point-in-time view of a task.
type TaskInfo struct {
	ID         string         `json:"id"`
	ParentID   string         `json:"parent_id,omitempty"`
	Kind       string         `json:"kind"`
	State      State          `json:"state"`
	Worker     int            `json:"worker,omitempty"`
	Value      any            `json:"value,omitempty"`
	Err        string         `json:"err,omitempty"`
	CreatedAt  time.Time      `json:"created_at"`
	StartedAt  *time.Time     `json:"started_at,omitempty"`
	FinishedAt *time.Time     `json:"finished_at,omitempty"`
	Attempts   int            `json:"attempts"`
	Detail     map[string]any `json:"detail,omitempty"`
}

// Result is Snapshot plus the raw Go error (nil unless the task failed).
type Result struct {
	TaskInfo
	Err error
}

// Stats are executor counters since construction.
type Stats struct {
	Workers      int  `json:"workers"`
	Submitted    int  `json:"submitted"`
	Spawned      int  `json:"spawned"`
	Started      int  `json:"started"`
	Succeeded    int  `json:"succeeded"`
	Failed       int  `json:"failed"`
	Panicked     int  `json:"panicked"`
	Canceled     int  `json:"canceled"`
	Running      int  `json:"running"`
	Outstanding  int  `json:"outstanding"`
	QueuedLocal  int  `json:"queued_local"`
	QueuedGlobal int  `json:"queued_global"`
	ShuttingDown bool `json:"shutting_down"`
}

// Errors returned by the executor.
var (
	// ErrExecutorShutdown is returned when work is submitted after
	// Shutdown has started.
	ErrExecutorShutdown = errors.New("scheduler: executor is shut down")
	// ErrKindNotFound is returned for an unregistered task kind.
	ErrKindNotFound = errors.New("scheduler: task kind not found")
	// ErrInvalidHandle is returned when a handle is nil or foreign.
	ErrInvalidHandle = errors.New("scheduler: invalid task handle")
	// ErrInvalidArgument is returned for malformed arguments.
	ErrInvalidArgument = errors.New("scheduler: invalid argument")
)

// Option configures a new Executor.
type Option func(*config)

type config struct {
	workers int
	clock   Clock
	sinks   []EventSink
	ctx     context.Context
	kinds   map[string]TaskFunc
}

// WithWorkers sets the fixed number of worker goroutines (n >= 1).
func WithWorkers(n int) Option {
	return func(c *config) { c.workers = n }
}

// WithClock installs a custom clock (default: real wall clock).
func WithClock(clock Clock) Option {
	return func(c *config) { c.clock = clock }
}

// WithSinks appends structured-event sinks.
func WithSinks(sinks ...EventSink) Option {
	return func(c *config) { c.sinks = append(c.sinks, sinks...) }
}

// WithContext sets the root context; every task context is derived
// from it, so canceling it cancels all tasks.
func WithContext(ctx context.Context) Option {
	return func(c *config) { c.ctx = ctx }
}

// WithKind registers a task kind before start.
func WithKind(name string, fn TaskFunc) Option {
	return func(c *config) { c.kinds[name] = fn }
}

// Executor is a fixed-pool work-stealing task executor.
type Executor struct {
	cfg     config
	clock   Clock
	sink    *fanout
	seq     seqSource
	locals  []*deque
	workers int

	rootCtx    context.Context
	rootCancel context.CancelFunc

	mu sync.Mutex
	cv *sync.Cond

	shuttingDown bool
	workersStop  bool // gates worker exit; set only by Shutdown
	closed       bool // Shutdown's wait loop has finished
	closedCV     *sync.Cond
	workerWG     sync.WaitGroup

	idGen       int64
	submitCount int
	spawnCount  int
	startCount  int
	stateCount  map[State]int

	tasks map[string]*task
	// global is a small overflow / root-task queue; workers pop from
	// random positions to avoid thundering-herd contention.
	global []*task

	// running counts tasks executing functions; outstanding counts
	// every non-terminal task (pending or running).
	running     int
	outstanding int
}

type task struct {
	id       string
	parentID string
	kind     string
	fn       Func
	payload  any

	ctx    context.Context
	cancel context.CancelFunc

	state    State
	done     chan struct{}
	value    any
	rawErr   error
	errMsg   string
	panicVal any

	worker     int
	attempts   int
	children   map[string]*task
	createdAt  time.Time
	startedAt  time.Time
	finishedAt time.Time
}

func asFunc(kind string, tf TaskFunc, payload any) Func {
	return func(ctx context.Context, rt Runtime) (any, error) {
		return tf(ctx, rt, payload)
	}
}

// New constructs an executor and starts its fixed worker pool.
func New(opts ...Option) (*Executor, error) {
	cfg := config{
		workers: runtime.NumCPU(),
		clock:   NewRealClock(),
		ctx:     context.Background(),
		kinds:   map[string]TaskFunc{},
	}
	for _, o := range opts {
		o(&cfg)
	}
	if cfg.workers < 1 {
		return nil, fmt.Errorf("%w: workers must be >= 1, got %d", ErrInvalidArgument, cfg.workers)
	}
	if cfg.clock == nil {
		cfg.clock = NewRealClock()
	}
	rootCtx, rootCancel := context.WithCancel(cfg.ctx)
	e := &Executor{
		cfg:        cfg,
		clock:      cfg.clock,
		workers:    cfg.workers,
		locals:     make([]*deque, cfg.workers),
		rootCtx:    rootCtx,
		rootCancel: rootCancel,
		tasks:      map[string]*task{},
		stateCount: map[State]int{},
	}
	e.cv = sync.NewCond(&e.mu)
	e.closedCV = sync.NewCond(&e.mu)
	for i := range e.locals {
		e.locals[i] = newDeque(64)
	}
	e.sink = newFanout(cfg.sinks...)
	for w := 1; w <= e.workers; w++ {
		e.workerWG.Add(1)
		go e.workerLoop(w)
	}
	return e, nil
}

// NumWorkers returns the fixed worker-pool size.
func (e *Executor) NumWorkers() int { return e.workers }

// RegisterKind adds a task kind. It is safe to call before serving
// traffic; concurrently with Submit is also safe.
func (e *Executor) RegisterKind(name string, fn TaskFunc) {
	e.mu.Lock()
	e.cfg.kinds[name] = fn
	e.mu.Unlock()
}

// Submit enqueues a root task of a registered kind.
func (e *Executor) Submit(kind string, payload any) (Handle, error) {
	tf, ok := e.lookupKind(kind)
	if !ok {
		return nil, fmt.Errorf("%w: %q", ErrKindNotFound, kind)
	}
	ctx, cancel := context.WithCancel(e.rootCtx)
	t := &task{
		kind:      kind,
		fn:        asFunc(kind, tf, payload),
		payload:   payload,
		ctx:       ctx,
		cancel:    cancel,
		state:     StatePending,
		done:      make(chan struct{}),
		children:  map[string]*task{},
		createdAt: e.clock.Now(),
	}

	e.mu.Lock()
	if e.shuttingDown {
		cancel()
		e.mu.Unlock()
		return nil, ErrExecutorShutdown
	}
	e.installTaskLocked(t, nil)
	e.outstanding++
	e.submitCount++
	w := rand.Intn(e.workers)
	e.locals[w].pushBottom(t)
	e.deliverLocked(e.makeEventLocked(EventSubmitted, t, 0, 0, nil))
	e.cv.Broadcast()
	e.mu.Unlock()

	return t, nil
}

// SubmitFunc enqueues a root task backed by an inline function.
func (e *Executor) SubmitFunc(name string, fn Func) (Handle, error) {
	if fn == nil {
		return nil, fmt.Errorf("%w: nil function", ErrInvalidArgument)
	}
	ctx, cancel := context.WithCancel(e.rootCtx)
	t := &task{
		kind:      name,
		fn:        fn,
		ctx:       ctx,
		cancel:    cancel,
		state:     StatePending,
		done:      make(chan struct{}),
		children:  map[string]*task{},
		createdAt: e.clock.Now(),
	}
	e.mu.Lock()
	if e.shuttingDown {
		cancel()
		e.mu.Unlock()
		return nil, ErrExecutorShutdown
	}
	e.installTaskLocked(t, nil)
	e.outstanding++
	e.submitCount++
	w := rand.Intn(e.workers)
	e.locals[w].pushBottom(t)
	e.deliverLocked(e.makeEventLocked(EventSubmitted, t, 0, 0, nil))
	e.cv.Broadcast()
	e.mu.Unlock()
	return t, nil
}

// Cancel requests cancellation of h and its whole descendant subtree.
// Running tasks have their contexts canceled cooperatively; pending
// descendants become "canceled" immediately. It returns false when the
// task was already terminal (no effect).
func (e *Executor) Cancel(h Handle) (bool, error) {
	t, err := e.asTask(h)
	if err != nil {
		return false, err
	}
	var evs []Event
	e.mu.Lock()
	if t.state.IsTerminal() {
		e.mu.Unlock()
		return false, nil
	}
	evs = append(evs, e.makeEventLocked(EventCanceled, t, 0, 0, nil))
	e.cancelSubtreeLocked([]*task{t}, &evs)
	for _, ev := range evs {
		e.deliverLocked(ev)
	}
	e.cv.Broadcast()
	e.mu.Unlock()
	return true, nil
}

// Shutdown stops accepting work, cancels all pending tasks and lets
// running tasks finish. If ctx is canceled before they finish, their
// contexts are canceled as a cooperation request; Shutdown then waits
// until the worker pool actually exits (a task that ignores its
// context therefore still blocks shutdown completion, by design).
// Shutdown is idempotent.
func (e *Executor) Shutdown(ctx context.Context) error {
	e.mu.Lock()
	if e.shuttingDown {
		for !e.closed {
			e.closedCV.Wait()
		}
		e.mu.Unlock()
		return nil
	}
	e.shuttingDown = true

	// Physically drain all queues, then mark every non-terminal task
	// terminal/canceled. Draining first guarantees no canceled task is
	// left occupying a queue (which would park workers forever).
	gathered := make([]*task, 0, len(e.global))
	gathered = append(gathered, e.global...)
	for _, d := range e.locals {
		for {
			t := d.popBottom()
			if t == nil {
				break
			}
			gathered = append(gathered, t)
		}
	}
	e.global = nil

	var evs []Event
	evs = append(evs, Event{
		Seq:  e.seq.next(),
		Time: e.clock.Now().Format(time.RFC3339Nano),
		Type: EventShutdown,
	})
	// Pending tasks (all queues were drained above) are canceled
	// immediately and their functions will never run. Running tasks
	// are left untouched during the graceful window: they keep their
	// context and finish naturally; only when the drain context
	// expires does rootCancel() ask them to stop cooperatively.
	e.cancelPendingLocked(gathered, &evs)
	for _, ev := range evs {
		e.deliverLocked(ev)
	}
	e.cv.Broadcast()
	e.mu.Unlock()

	// Wait for the pool to drain. On deadline/timeout, request
	// cooperative cancellation of all running work via the root
	// context, but keep waiting for actual exit.
	poolExit := make(chan struct{})
	go func() {
		e.workerWG.Wait()
		close(poolExit)
	}()
	forceDone := false
	for {
		e.mu.Lock()
		if e.closed {
			e.mu.Unlock()
			break
		}
		if e.running == 0 && e.totalQueuedLocked() == 0 {
			// Pool is empty: release workers and finish. Whether this
			// counts as graceful or forced depends on forceDone,
			// recorded in the return value below.
			e.workersStop = true
			e.closed = true
			e.closedCV.Broadcast()
			e.cv.Broadcast()
			e.mu.Unlock()
			break
		}
		if ctx.Err() != nil {
			// Drain deadline elapsed: ask running tasks to stop
			// cooperatively via the root context, keep waiting for
			// real exit.
			if !forceDone {
				e.rootCancel()
				forceDone = true
			}
			e.cv.Broadcast()
			e.mu.Unlock()
			select {
			case <-poolExit:
			case <-time.After(10 * time.Millisecond):
				// Poll to re-enter the locked accounting check; this
				// also bounds how long we block here when workers need
				// the lock to make progress.
			}
			continue
		}
		e.cv.Broadcast() // wake parked workers in case a signal raced
		e.mu.Unlock()

		select {
		case <-poolExit:
		case <-ctx.Done():
			// Re-enter the loop; the locked branch above performs the
			// rootCancel and remembers forceDone.
		}
	}
	e.workerWG.Wait()
	e.sink.close()
	if forceDone {
		return ctx.Err()
	}
	return nil
}

// WaitIdle blocks until no non-terminal tasks remain. It is primarily
// useful in tests.
func (e *Executor) WaitIdle(ctx context.Context) error {
	stop := context.AfterFunc(ctx, func() {
		e.mu.Lock()
		e.cv.Broadcast()
		e.mu.Unlock()
	})
	defer stop()
	e.mu.Lock()
	for e.outstanding != 0 || e.totalQueuedLocked() != 0 {
		if err := ctx.Err(); err != nil {
			e.mu.Unlock()
			return err
		}
		e.cv.Wait()
	}
	e.mu.Unlock()
	return nil
}

// Stats returns a snapshot of executor counters.
func (e *Executor) Stats() Stats {
	e.mu.Lock()
	defer e.mu.Unlock()
	s := Stats{
		Workers:      e.workers,
		Submitted:    e.submitCount,
		Spawned:      e.spawnCount,
		Started:      e.startCount,
		Succeeded:    e.stateCount[StateSucceeded],
		Failed:       e.stateCount[StateFailed],
		Panicked:     e.stateCount[StatePanicked],
		Canceled:     e.stateCount[StateCanceled],
		Running:      e.running,
		Outstanding:  e.outstanding,
		QueuedGlobal: len(e.global),
		ShuttingDown: e.shuttingDown,
	}
	for _, d := range e.locals {
		s.QueuedLocal += d.len()
	}
	return s
}

// Snapshot returns the state of one task.
func (e *Executor) Snapshot(h Handle) (TaskInfo, error) {
	t, err := e.asTask(h)
	if err != nil {
		return TaskInfo{}, err
	}
	e.mu.Lock()
	info := t.infoLocked()
	e.mu.Unlock()
	return info, nil
}

// Result waits for terminal state (without helping; this is intended
// for non-worker callers such as the HTTP layer) and returns the full
// result including the raw Go error.
func (e *Executor) Result(ctx context.Context, h Handle) (Result, error) {
	t, err := e.asTask(h)
	if err != nil {
		return Result{}, err
	}
	if ctx == nil {
		ctx = context.Background()
	}
	select {
	case <-t.done:
	case <-ctx.Done():
		return Result{}, ctx.Err()
	}
	e.mu.Lock()
	info := t.infoLocked()
	rawErr := t.rawErr
	e.mu.Unlock()
	return Result{TaskInfo: info, Err: rawErr}, nil
}

// ListTasks returns snapshots of all retained tasks (newest first).
func (e *Executor) ListTasks(limit int) []TaskInfo {
	e.mu.Lock()
	if limit <= 0 || limit > len(e.tasks) {
		limit = len(e.tasks)
	}
	out := make([]TaskInfo, 0, limit)
	for _, t := range e.tasks {
		if len(out) >= limit {
			break
		}
		out = append(out, t.infoLocked())
	}
	e.mu.Unlock()
	return out
}

// ---------------------------------------------------------------------------
// Internal: task lifecycle
// ---------------------------------------------------------------------------

func (e *Executor) lookupKind(kind string) (TaskFunc, bool) {
	e.mu.Lock()
	tf, ok := e.cfg.kinds[kind]
	e.mu.Unlock()
	return tf, ok
}

// installTaskLocked assigns an id and indexes the task, linking it to
// its (still non-terminal) parent's children set.
func (e *Executor) installTaskLocked(t *task, parent *task) {
	e.idGen++
	t.id = fmt.Sprintf("t-%d", e.idGen)
	if parent != nil {
		t.parentID = parent.id
		parent.children[t.id] = t
	}
	e.tasks[t.id] = t
}

func (e *Executor) asTask(h Handle) (*task, error) {
	if h == nil {
		return nil, ErrInvalidHandle
	}
	t, ok := h.(*task)
	if !ok {
		return nil, ErrInvalidHandle
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	if _, exists := e.tasks[t.id]; !exists {
		return nil, ErrInvalidHandle
	}
	return t, nil
}

// HandleByID looks up a retained task by its string id, returning nil
// when unknown.
func (e *Executor) HandleByID(id string) Handle {
	e.mu.Lock()
	defer e.mu.Unlock()
	if t, ok := e.tasks[id]; ok {
		return t
	}
	return nil
}

// cancelSubtreeLocked marks every non-terminal descendant of roots
// terminal: pending tasks become canceled immediately (their function
// never runs), running tasks receive context cancellation and keep
// running until they observe it. Events for every newly terminal task
// are appended to evs.
func (e *Executor) cancelSubtreeLocked(roots []*task, evs *[]Event) {
	stack := append([]*task(nil), roots...)
	for len(stack) > 0 {
		t := stack[len(stack)-1]
		stack = stack[:len(stack)-1]
		if t.state.IsTerminal() {
			continue
		}
		// Request context cancellation unconditionally (covers both
		// running tasks and the derived contexts of pending ones).
		t.cancel()
		if t.state == StatePending {
			t.state = StateCanceled
			t.finishedAt = e.clock.Now()
			e.stateCount[StateCanceled]++
			e.outstanding--
			close(t.done)
			*evs = append(*evs, e.makeEventLocked(EventCompleted, t, 0, 0, nil))
		}
		// running: state decided when the function returns.
		for _, ch := range t.children {
			if !ch.state.IsTerminal() {
				stack = append(stack, ch)
			}
		}
	}
}

// cancelPendingLocked finalizes tasks known to be pending (their
// functions have not started and never will): their contexts are
// canceled, state moves to canceled and completion events are
// appended. Already-terminal or currently-running tasks are
// skipped.
func (e *Executor) cancelPendingLocked(roots []*task, evs *[]Event) {
	stack := append([]*task(nil), roots...)
	for len(stack) > 0 {
		t := stack[len(stack)-1]
		stack = stack[:len(stack)-1]
		if t.state != StatePending {
			// Descend anyway to catch pending children of running
			// tasks that were in drained queues.
			for _, ch := range t.children {
				stack = append(stack, ch)
			}
			continue
		}
		t.cancel()
		t.state = StateCanceled
		t.finishedAt = e.clock.Now()
		e.stateCount[StateCanceled]++
		e.outstanding--
		close(t.done)
		*evs = append(*evs, e.makeEventLocked(EventCompleted, t, 0, 0, nil))
		for _, ch := range t.children {
			stack = append(stack, ch)
		}
	}
}

// completeLocked finalizes a task whose function has returned.
func (e *Executor) completeLocked(t *task, value any, fnErr error, panicked any, evs *[]Event) {
	switch {
	case panicked != nil:
		t.state = StatePanicked
		t.panicVal = panicked
		t.errMsg = fmt.Sprintf("panic: %v", panicked)
	case fnErr != nil:
		// A task that returns its own (possibly wrapped) context
		// cancellation error is recorded as canceled rather than
		// failed: the trigger was cancellation, not an ordinary work
		// failure. Explicit application errors unrelated to the
		// context remain failures even when the context is canceled.
		if isCancellationError(fnErr) {
			t.state = StateCanceled
			t.errMsg = fnErr.Error()
		} else {
			t.state = StateFailed
			t.rawErr = fnErr
			t.errMsg = fnErr.Error()
		}
	case t.ctx.Err() != nil:
		// Returned cleanly after its context was canceled: canceled.
		t.state = StateCanceled
		t.errMsg = t.ctx.Err().Error()
	default:
		t.state = StateSucceeded
		t.value = value
	}
	t.finishedAt = e.clock.Now()
	e.stateCount[t.state]++
	e.running--
	e.outstanding--
	if t.parentID != "" {
		if p := e.tasks[t.parentID]; p != nil {
			delete(p.children, t.id)
		}
	}
	if t.state == StatePanicked {
		stack := debug.Stack()
		if len(stack) > 4096 {
			stack = stack[:4096]
		}
		*evs = append(*evs, e.makeEventLocked(EventCompleted, t, 0, 0, map[string]any{
			"panic": fmt.Sprint(panicked),
			"stack": string(stack),
		}))
	} else {
		*evs = append(*evs, e.makeEventLocked(EventCompleted, t, 0, 0, nil))
	}
	close(t.done)
	e.cv.Broadcast()
}

func (e *Executor) totalQueuedLocked() int {
	n := len(e.global)
	for _, d := range e.locals {
		d.mu.Lock()
		n += d.size
		d.mu.Unlock()
	}
	return n
}

func (e *Executor) workAvailableLocked() bool {
	if len(e.global) > 0 {
		return true
	}
	for _, d := range e.locals {
		d.mu.Lock()
		n := d.size
		d.mu.Unlock()
		if n > 0 {
			return true
		}
	}
	return false
}

// ---------------------------------------------------------------------------
// Worker loop and work acquisition
// ---------------------------------------------------------------------------

func (e *Executor) workerLoop(id int) {
	defer e.workerWG.Done()
	for {
		if ran, _ := e.runOne(id); ran {
			continue
		}
		// No task found: park until work is pushed, a task completes
		// (a waiting worker may now help) or shutdown drains.
		e.mu.Lock()
		for {
			if e.workersStop && e.running == 0 && e.totalQueuedLocked() == 0 {
				e.mu.Unlock()
				return
			}
			if e.workAvailableLocked() {
				break
			}
			e.cv.Wait()
		}
		e.mu.Unlock()
	}
}

// runOne tries to acquire and execute one task. It returns true if a
// queued task was consumed (even if it turned out to be already
// canceled in flight). victim is the 1-based source worker id when the
// task was stolen, else 0.
func (e *Executor) runOne(worker int) (ran bool, victim int) {
	t, victim := e.getWork(worker)
	if t == nil {
		return false, 0
	}
	e.mu.Lock()
	if t.state.IsTerminal() {
		// Canceled while in flight between pop and claim; its
		// finalization already happened. Just drop it.
		e.mu.Unlock()
		return true, 0
	}
	t.state = StateRunning
	t.worker = worker
	t.attempts++
	t.startedAt = e.clock.Now()
	e.running++
	e.startCount++
	e.deliverLocked(e.makeEventLocked(EventStarted, t, worker, victim, nil))
	if victim != 0 {
		e.deliverLocked(e.makeEventLocked(EventStole, t, worker, victim, nil))
	}
	e.mu.Unlock()

	e.execute(t, worker)
	return true, victim
}

// getWork implements the acquisition order:
//  1. own local deque (LIFO bottom),
//  2. global queue (random position),
//  3. steal half of a random victim's deque (top / oldest side).
func (e *Executor) getWork(worker int) (*task, int) {
	idx := worker - 1
	if t := e.locals[idx].popBottom(); t != nil {
		return t, 0
	}

	e.mu.Lock()
	if n := len(e.global); n > 0 {
		i := rand.Intn(n)
		t := e.global[i]
		e.global[i] = e.global[n-1]
		e.global[n-1] = nil
		e.global = e.global[:n-1]
		e.mu.Unlock()
		return t, 0
	}
	e.mu.Unlock()

	if e.workers < 2 {
		return nil, 0
	}
	start := rand.Intn(e.workers - 1)
	for off := 0; off < e.workers-1; off++ {
		v := (start + off) % e.workers
		if v == idx {
			v = (v + 1) % e.workers
		}
		d := e.locals[v]
		n := d.len()
		if n == 0 {
			continue
		}
		max := (n + 1) / 2
		buf := make([]*task, max)
		got := d.stealUpTo(max, buf)
		if got == 0 {
			continue
		}
		run := buf[0]
		for i := 1; i < got; i++ {
			e.locals[idx].pushBottom(buf[i])
		}
		return run, v + 1
	}
	return nil, 0
}

func (e *Executor) execute(t *task, worker int) {
	rt := &taskRuntime{exec: e, worker: worker, task: t}
	var (
		val      any
		fnErr    error
		panicked any
	)
	func() {
		defer func() { panicked = recover() }()
		val, fnErr = t.fn(t.ctx, rt)
	}()
	var evs []Event
	e.mu.Lock()
	e.completeLocked(t, val, fnErr, panicked, &evs)
	for _, ev := range evs {
		e.deliverLocked(ev)
	}
	e.mu.Unlock()
}

// ---------------------------------------------------------------------------
// Event helpers
// ---------------------------------------------------------------------------

func (e *Executor) makeEventLocked(typ EventType, t *task, worker, from int, detail map[string]any) Event {
	ev := Event{
		Seq:    e.seq.next(),
		Time:   e.clock.Now().Format(time.RFC3339Nano),
		Type:   typ,
		Kind:   t.kind,
		Worker: worker,
		From:   from,
	}
	if t != nil {
		ev.TaskID = t.id
		ev.ParentID = t.parentID
		switch typ {
		case EventStarted:
			ev.State = StateRunning
		case EventCompleted:
			ev.State = t.state
			if t.errMsg != "" {
				ev.Err = t.errMsg
			}
		case EventSubmitted, EventSpawned:
			ev.State = StatePending
		}
	}
	if detail != nil {
		ev.Detail = detail
	}
	return ev
}

// deliverLocked hands an already-constructed event to the ordered
// emitter while the executor lock is held, which is what makes
// delivery order identical to process-wide sequence order.
func (e *Executor) deliverLocked(ev Event) {
	e.sink.Record(ev)
}

func (t *task) ID() string            { return t.id }
func (t *task) Done() <-chan struct{} { return t.done }
func (t *task) Snapshot() TaskInfo {
	// Handle method without the executor lock; fields relevant to
	// callers after Done() are set-before-close, so a direct read of
	// immutable-at-terminal fields is safe.
	return TaskInfo{
		ID:       t.id,
		ParentID: t.parentID,
		Kind:     t.kind,
		State:    t.state,
		Worker:   t.worker,
		Value:    t.value,
		Err:      t.errMsg,
		Attempts: t.attempts,
	}
}

func (t *task) infoLocked() TaskInfo {
	info := TaskInfo{
		ID:        t.id,
		ParentID:  t.parentID,
		Kind:      t.kind,
		State:     t.state,
		Worker:    t.worker,
		Value:     t.value,
		Err:       t.errMsg,
		CreatedAt: t.createdAt,
		Attempts:  t.attempts,
	}
	if !t.startedAt.IsZero() {
		ts := t.startedAt
		info.StartedAt = &ts
	}
	if !t.finishedAt.IsZero() {
		ts := t.finishedAt
		info.FinishedAt = &ts
	}
	return info
}
