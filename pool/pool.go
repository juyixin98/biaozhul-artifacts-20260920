package pool

import (
	"context"
	"fmt"
	"strconv"
	"sync"
	"sync/atomic"
	"time"
)

// RejectPolicy decides what happens to a submission when the bounded queue
// (counting tasks already scheduled for later) is full.
type RejectPolicy int

const (
	// RejectAbort returns ErrQueueFull; the caller keeps the task.
	RejectAbort RejectPolicy = iota
	// RejectCallerRuns runs the task synchronously on the Submit caller's
	// goroutine, providing back-pressure. The task still settles exactly
	// once through the returned Future.
	RejectCallerRuns
	// RejectDiscard silently drops the newest (current) submission.
	RejectDiscard
	// RejectDiscardOldest evicts the oldest queued task (settled as
	// canceled) and retries the submission.
	RejectDiscardOldest
)

func (p RejectPolicy) String() string {
	switch p {
	case RejectCallerRuns:
		return "caller_runs"
	case RejectDiscard:
		return "discard"
	case RejectDiscardOldest:
		return "discard_oldest"
	default:
		return "abort"
	}
}

// Config configures a Pool.
type Config struct {
	// Name labels the pool and every event emitted from it.
	Name string
	// Workers is the initial worker count. Must be 0..MaxWorkers.
	Workers int
	// QueueCapacity bounds accepted-but-not-finished queued work plus tasks
	// scheduled for later. Must be >= 1.
	QueueCapacity int
	// Policy decides behaviour when the queue is full. Default Abort.
	Policy RejectPolicy
	// Clock abstracts time (defaults to RealClock).
	Clock Clock
	// Executor runs task bodies (defaults to DirectExecutor).
	Executor Executor
	// Sink receives structured events (defaults to NopSink).
	Sink Sink
}

// MaxWorkers is the largest supported worker count.
const MaxWorkers = 1 << 14

// Stats is a point-in-time pool snapshot.
type Stats struct {
	Name            string `json:"name"`
	State           string `json:"state"`
	DesiredWorkers  int    `json:"desired_workers"`
	ActiveWorkers   int    `json:"active_workers"`
	RetiringWorkers int    `json:"retiring_workers"`
	QueueLen        int    `json:"queue_len"`
	QueueCap        int    `json:"queue_cap"`
	AcceptDelayed   int    `json:"scheduled"`
	Accepted        int64  `json:"accepted"`
	Rejected        int64  `json:"rejected"`
	Started         int64  `json:"started"`
	Completed       int64  `json:"completed"`
	Failed          int64  `json:"failed"`
	Canceled        int64  `json:"canceled"`
	RunningNow      int    `json:"running_now"`
}

type poolStatus int

const (
	statusRunning poolStatus = iota
	statusShuttingDown
	statusForce
	statusTerminated
)

func (s poolStatus) String() string {
	switch s {
	case statusShuttingDown:
		return "shutting_down"
	case statusForce:
		return "force_canceling"
	case statusTerminated:
		return "terminated"
	default:
		return "running"
	}
}

// Pool is a bounded-queue, dynamically resizable worker pool. All methods are
// safe for concurrent use.
type Pool struct {
	cfg      Config
	clock    Clock
	executor Executor
	sink     Sink

	mu       sync.Mutex
	status   poolStatus
	queue    *ring
	desired  int
	active   int // live worker goroutines (incl. not-yet-started and drainers)
	spawning int // spawned but still entering their loop
	running  int // workers currently inside a task function
	// retiring counts shrink notices not yet picked up by an idle worker.
	// Effective serving workers = active - spawning - retiring. A running
	// task never observes the notice (idle workers only check it after a
	// pop attempt fails), so shrink can never interrupt in-flight work.
	retiring int
	// parked maps a worker id to its signal slot, registered only while the
	// worker is about to block. wakeAllLocked() (called on submit / resize /
	// shutdown under mu) sends a non-blocking tick to every slot. Because
	// registration and the state check share the same lock, a signal that
	// arrives after the decision but before blocking is still sitting in the
	// slot — no wake-up can ever be lost.
	parked map[int64]chan struct{}
	// watchCh wakes the termination watcher (cap 1).
	watchCh chan struct{}

	stopCh      chan struct{} // closed on any shutdown
	forceCh     chan struct{} // closed on ShutdownNow
	doneCh      chan struct{} // closed at termination
	stopClosed  bool
	forceClosed bool

	// workersWG is Done only after a worker (or the internal drainer) has
	// fully returned, which includes emitting its worker.stopped event. The
	// termination watcher waits on this so shutdown never returns before the
	// final stop events are observable.
	workersWG sync.WaitGroup

	scheduled map[int64]*schedEntry

	workerSeq  int64
	schedSeq   int64
	taskIDSeq  int64
	watchArmed bool

	accepted  int64
	rejected  int64
	started   int64
	completed int64
	failed    int64
	canceled  int64

	futures map[string]*Future
}

type schedEntry struct {
	fut      *Future
	task     Task
	deadline time.Time
	timer    Timer
}

// New constructs a pool and starts Workers worker goroutines.
func New(cfg Config) (*Pool, error) {
	if cfg.QueueCapacity < 1 {
		return nil, fmt.Errorf("%w: queue capacity must be >= 1, got %d", ErrInvalidConfig, cfg.QueueCapacity)
	}
	if cfg.Workers < 0 || cfg.Workers > MaxWorkers {
		return nil, fmt.Errorf("%w: workers must be in [0,%d], got %d", ErrInvalidConfig, MaxWorkers, cfg.Workers)
	}
	if cfg.Name == "" {
		cfg.Name = "pool"
	}
	if cfg.Clock == nil {
		cfg.Clock = NewRealClock()
	}
	if cfg.Executor == nil {
		cfg.Executor = NewDirectExecutor()
	}
	if cfg.Sink == nil {
		cfg.Sink = NopSink
	}
	p := &Pool{
		cfg:       cfg,
		clock:     cfg.Clock,
		executor:  cfg.Executor,
		sink:      cfg.Sink,
		queue:     newRing(cfg.QueueCapacity),
		desired:   cfg.Workers,
		stopCh:    make(chan struct{}),
		forceCh:   make(chan struct{}),
		doneCh:    make(chan struct{}),
		scheduled: make(map[int64]*schedEntry),
		parked:    make(map[int64]chan struct{}),
		watchCh:   make(chan struct{}, 1),
		futures:   make(map[string]*Future),
	}
	now := p.clock.Now()
	// Emit pool.created before any worker can start, so the event stream
	// always begins with it.
	p.emit(Event{Time: now, Kind: EventPoolCreated, Pool: cfg.Name,
		Current: 0, Desired: cfg.Workers, QueueCap: cfg.QueueCapacity,
		Extra: map[string]any{"policy": cfg.Policy.String()}})
	p.mu.Lock()
	ids := p.spawnLocked(cfg.Workers)
	p.mu.Unlock()
	p.launchWorkers(ids)
	return p, nil
}

// Name returns the configured pool name.
func (p *Pool) Name() string { return p.cfg.Name }

// effectiveWorkers is the number of workers serving tasks: live goroutines
// minus not-yet-started spawns minus outstanding retirement notices. The
// result is clamped at 0 because during shutdown outstanding notices are
// discarded while workers exit via the drain path.
func (p *Pool) effectiveWorkers() int {
	n := p.active - p.spawning - p.retiring
	if n < 0 {
		return 0
	}
	return n
}

// ---------------------------------------------------------------- submission

// Submit accepts a task and returns its Future.
//
// Accepted tasks run exactly once and always settle (completed, failed or
// canceled): a graceful shutdown still drains them, and a force cancel settles
// unstarted ones as canceled. Rejection is reported both on the returned
// future and as an error.
func (p *Pool) Submit(t Task) (*Future, error) {
	if t.Fn == nil {
		return nil, fmt.Errorf("%w: task fn is nil", ErrInvalidConfig)
	}
	if t.ID == "" {
		t.ID = p.genID()
	}
	now := p.clock.Now()

	p.mu.Lock()
	if p.status != statusRunning {
		fut := newFuture(t.ID, t.Type, now)
		p.markRejectedLocked(fut)
		p.mu.Unlock()
		p.emit(Event{Time: now, Kind: EventTaskRejected, Pool: p.cfg.Name, TaskID: t.ID,
			Reason: "pool_" + p.status.String(), Err: ErrPoolShuttingDown.Error()})
		return fut, ErrPoolShuttingDown
	}
	if !p.queue.full() {
		fut := newFuture(t.ID, t.Type, now)
		p.acceptLocked(workItem{task: t, fut: fut})
		ev := Event{Time: now, Kind: EventTaskSubmitted, Pool: p.cfg.Name, TaskID: t.ID,
			QueueLen: p.queue.len(), QueueCap: p.cfg.QueueCapacity}
		p.mu.Unlock()
		p.emit(ev)
		return fut, nil
	}
	policy := p.cfg.Policy
	p.mu.Unlock()

	switch policy {
	case RejectCallerRuns:
		fut := newFuture(t.ID, t.Type, now)
		p.runInline(t, fut)
		return fut, nil
	case RejectDiscard:
		fut := newFuture(t.ID, t.Type, now)
		p.reject(fut, "discard", ErrTaskDiscarded)
		return fut, ErrTaskDiscarded
	case RejectDiscardOldest:
		return p.discardOldest(t)
	default:
		fut := newFuture(t.ID, t.Type, now)
		p.reject(fut, "abort", ErrQueueFull)
		return fut, ErrQueueFull
	}
}

func (p *Pool) acceptLocked(it workItem) {
	p.queue.push(it)
	p.futures[it.fut.id] = it.fut
	p.accepted++
	p.wakeAllLocked()
}

func (p *Pool) markRejectedLocked(fut *Future) {
	p.futures[fut.id] = fut
	p.rejected++
	fut.transition(StateRejected, nil, ErrPoolShuttingDown, p.clock.Now(), 0)
}

func (p *Pool) reject(fut *Future, reason string, err error) {
	now := p.clock.Now()
	p.mu.Lock()
	p.futures[fut.id] = fut
	p.rejected++
	fut.transition(StateRejected, nil, err, now, 0)
	ev := Event{Time: now, Kind: EventTaskRejected, Pool: p.cfg.Name, TaskID: fut.id,
		Reason: reason, QueueLen: p.cfg.QueueCapacity, QueueCap: p.cfg.QueueCapacity,
		Err: err.Error()}
	p.mu.Unlock()
	p.emit(ev)
}

func (p *Pool) discardOldest(t Task) (*Future, error) {
	now := p.clock.Now()
	p.mu.Lock()
	if p.status != statusRunning {
		fut := newFuture(t.ID, t.Type, now)
		p.markRejectedLocked(fut)
		p.mu.Unlock()
		return fut, ErrPoolShuttingDown
	}
	if !p.queue.full() {
		fut := newFuture(t.ID, t.Type, now)
		p.acceptLocked(workItem{task: t, fut: fut})
		ev := Event{Time: now, Kind: EventTaskSubmitted, Pool: p.cfg.Name, TaskID: t.ID,
			QueueLen: p.queue.len(), QueueCap: p.cfg.QueueCapacity}
		p.mu.Unlock()
		p.emit(ev)
		return fut, nil
	}
	old, _ := p.queue.pop()
	p.canceled++
	cbsOld := old.fut.transition(StateCanceled, nil, ErrTaskDiscarded, now, 0)
	fut := newFuture(t.ID, t.Type, now)
	p.acceptLocked(workItem{task: t, fut: fut})
	evOld := Event{Time: now, Kind: EventTaskCanceled, Pool: p.cfg.Name, TaskID: old.fut.id,
		Reason: "discard_oldest", Err: ErrTaskDiscarded.Error()}
	evNew := Event{Time: now, Kind: EventTaskSubmitted, Pool: p.cfg.Name, TaskID: t.ID,
		QueueLen: p.queue.len(), QueueCap: p.cfg.QueueCapacity, Reason: "discard_oldest"}
	p.mu.Unlock()
	fire(cbsOld)
	p.emit(evOld)
	p.emit(evNew)
	return fut, nil
}

// runInline executes a task on the caller's goroutine (caller-runs policy).
func (p *Pool) runInline(t Task, fut *Future) {
	now := p.clock.Now()
	p.mu.Lock()
	p.futures[fut.id] = fut
	p.accepted++
	p.started++
	p.running++
	fut.transition(StateRunning, nil, nil, now, callerRunsWorkerID)
	p.mu.Unlock()
	p.emit(Event{Time: now, Kind: EventTaskSubmitted, Pool: p.cfg.Name, TaskID: fut.id,
		Reason: "caller_runs", QueueCap: p.cfg.QueueCapacity})
	p.emit(Event{Time: now, Kind: EventTaskStarted, Pool: p.cfg.Name, TaskID: fut.id,
		WorkerID: callerRunsWorkerID})

	res, err := p.executeOnce(chanCtx{parent: context.Background(), done: p.forceCh}, t)

	finish := p.clock.Now()
	p.mu.Lock()
	p.running--
	state := StateCompleted
	if err != nil {
		state = StateFailed
		p.failed++
	} else {
		p.completed++
	}
	cbs := fut.transition(state, res, err, finish, callerRunsWorkerID)
	completed := p.completed
	p.mu.Unlock()
	fire(cbs)
	kind := EventTaskCompleted
	if err != nil {
		kind = EventTaskFailed
	}
	p.emit(Event{Time: finish, Kind: kind, Pool: p.cfg.Name, TaskID: fut.id,
		WorkerID: callerRunsWorkerID, Completed: completed, Err: errStr(err)})
}

const callerRunsWorkerID int64 = -1

// ---------------------------------------------------------------- scheduling

// Schedule accepts a task to run no earlier than delay. Scheduled tasks count
// toward queue capacity until they start. During a graceful shutdown pending
// timers are stopped and the tasks are immediately queued for draining.
func (p *Pool) Schedule(t Task, delay time.Duration) (*Future, error) {
	if t.Fn == nil {
		return nil, fmt.Errorf("%w: task fn is nil", ErrInvalidConfig)
	}
	if delay < 0 {
		delay = 0
	}
	if t.ID == "" {
		t.ID = p.genID()
	}
	now := p.clock.Now()

	p.mu.Lock()
	if p.status != statusRunning {
		fut := newFuture(t.ID, t.Type, now)
		p.markRejectedLocked(fut)
		p.mu.Unlock()
		return fut, ErrPoolShuttingDown
	}
	if p.queue.len()+len(p.scheduled) >= p.cfg.QueueCapacity {
		policy := p.cfg.Policy
		p.mu.Unlock()
		// Capacity exhausted: apply policy to the task itself.
		switch policy {
		case RejectDiscard:
			fut := newFuture(t.ID, t.Type, now)
			p.reject(fut, "discard", ErrTaskDiscarded)
			return fut, ErrTaskDiscarded
		case RejectCallerRuns:
			fut := newFuture(t.ID, t.Type, now)
			p.runInline(t, fut)
			return fut, nil
		case RejectDiscardOldest:
			return p.discardOldest(t)
		default:
			fut := newFuture(t.ID, t.Type, now)
			p.reject(fut, "abort", ErrQueueFull)
			return fut, ErrQueueFull
		}
	}
	fut := newFuture(t.ID, t.Type, now)
	fut.setState(StateScheduled)
	p.futures[fut.id] = fut
	p.accepted++
	p.schedSeq++
	id := p.schedSeq
	entry := &schedEntry{fut: fut, task: t, deadline: now.Add(delay)}
	entry.timer = p.clock.NewTimer(delay)
	p.scheduled[id] = entry
	ev := Event{Time: now, Kind: EventTaskSubmitted, Pool: p.cfg.Name, TaskID: t.ID,
		Reason: "scheduled", QueueCap: p.cfg.QueueCapacity,
		Extra: map[string]any{"delay_ms": delay.Milliseconds()}}
	p.mu.Unlock()
	p.emit(ev)

	go p.scheduleWaiter(id, entry)
	return fut, nil
}

func (p *Pool) scheduleWaiter(id int64, entry *schedEntry) {
	select {
	case <-entry.timer.C():
	case <-p.stopCh:
	}
	p.mu.Lock()
	if cur, ok := p.scheduled[id]; !ok || cur != entry {
		p.mu.Unlock()
		return
	}
	delete(p.scheduled, id)
	now := p.clock.Now()
	if p.status == statusForce || p.status == statusTerminated {
		p.canceled++
		cbs := entry.fut.transition(StateCanceled, nil, ErrTaskCanceled, now, 0)
		p.wakeAllLocked()
		p.mu.Unlock()
		fire(cbs)
		p.emit(Event{Time: now, Kind: EventTaskCanceled, Pool: p.cfg.Name,
			TaskID: entry.fut.id, Reason: "shutdown_now"})
		return
	}
	entry.fut.transition(StateQueued, nil, nil, now, 0)
	p.queue.push(workItem{task: entry.task, fut: entry.fut})
	p.wakeAllLocked()
	p.wakeAllLocked()
	ev := Event{Time: now, Kind: EventTaskScheduled, Pool: p.cfg.Name,
		TaskID: entry.fut.id, QueueLen: p.queue.len()}
	p.mu.Unlock()
	p.emit(ev)
}

// ---------------------------------------------------------------- resizing

// Resize sets the desired number of workers.
//
// Growth spawns workers immediately. Shrink hands out retirement tokens that
// idle workers consume; a worker executing a task always finishes it first and
// only retires once it becomes idle. Resize returns as soon as the new target
// has been committed and the worker set is converging. Repeated resizes are
// coalesced: an unconsumed shrink token is reclaimed by a later growth.
func (p *Pool) Resize(workers int) error {
	if workers < 0 || workers > MaxWorkers {
		return fmt.Errorf("%w: workers must be in [0,%d], got %d", ErrInvalidConfig, MaxWorkers, workers)
	}
	now := p.clock.Now()
	p.mu.Lock()
	if p.status != statusRunning {
		p.mu.Unlock()
		return ErrPoolShuttingDown
	}
	old := p.desired
	p.desired = workers

	// committed is the serving capacity we are guaranteed to have: every
	// live worker minus those already slated to retire. Workers still in
	// flight (spawning) are included because they always enter service;
	// counting them prevents a duplicate spawn when Resize races startup.
	committed := p.active - p.retiring
	spawned, reclaimed, retired := 0, 0, 0
	var pendingIDs []int64
	switch {
	case workers > committed:
		need := workers - committed
		// A later growth first cancels shrink notices no idle worker has
		// claimed, then spawns only the remainder.
		if p.retiring >= need {
			p.retiring -= need
			reclaimed = need
			need = 0
		} else {
			reclaimed = p.retiring
			need -= p.retiring
			p.retiring = 0
		}
		if need > 0 {
			pendingIDs = p.spawnLocked(need)
			spawned = need
		}
	case workers < committed:
		n := committed - workers
		p.retiring += n
		retired = n
	}
	// Parked workers must wake to claim notices or new work.
	p.wakeAllLocked()
	ev := Event{Time: now, Kind: EventPoolResizing, Pool: p.cfg.Name,
		Current: p.effectiveWorkers(), Desired: workers, QueueLen: p.queue.len(),
		QueueCap: p.cfg.QueueCapacity,
		Extra: map[string]any{"old_desired": old, "spawned": spawned,
			"reclaimed_notices": reclaimed, "retirement_notices": retired}}
	p.mu.Unlock()
	p.launchWorkers(pendingIDs)
	p.emit(ev)
	return nil
}

// ---------------------------------------------------------------- shutdown

// Shutdown begins a graceful shutdown:
//   - new submissions are rejected;
//   - pending delayed tasks are queued immediately;
//   - workers finish every queued task, then exit (a Resize(0) before shutdown
//     still drains accepted work via an internal drainer).
//
// It waits for full termination, or returns ErrShutdownTimeout if ctx expires
// first (draining continues; use AwaitTermination to wait again).
func (p *Pool) Shutdown(ctx context.Context) error {
	now := p.clock.Now()
	p.mu.Lock()
	first := p.status == statusRunning
	if first {
		p.status = statusShuttingDown
		p.closeStopLocked()
		p.queueScheduledLocked(now)
		// Outstanding shrink notices are moot during shutdown: workers now
		// exit via the drain path regardless of desired size.
		p.retiring = 0
		p.wakeAllLocked()
		p.armWatcherLocked()
	}
	queueLen := p.queue.len()
	p.mu.Unlock()
	if first {
		p.emit(Event{Time: now, Kind: EventPoolShutdown, Pool: p.cfg.Name, QueueLen: queueLen})
	}
	return p.awaitDone(ctx)
}

// ShutdownNow force-cancels the pool:
//   - new submissions are rejected;
//   - contexts of currently running tasks are canceled (cooperative: tasks
//     must observe ctx.Done to stop early; tasks that ignore it still run to
//     completion — they are never killed mid-flight, because that would risk
//     corrupted task state);
//   - queued and not-yet-due tasks never run and settle as canceled;
//   - workers exit as soon as their current task returns.
//
// It returns snapshots of the tasks that were prevented from running and waits
// for worker exit (or ctx expiry).
func (p *Pool) ShutdownNow(ctx context.Context) ([]TaskSnapshot, error) {
	now := p.clock.Now()
	var notRun []TaskSnapshot
	p.mu.Lock()
	first := p.status != statusTerminated
	if first {
		if p.status == statusRunning {
			p.status = statusShuttingDown
			p.closeStopLocked()
			p.queueScheduledLocked(now)
		}
		p.status = statusForce
		p.retiring = 0
		if !p.forceClosed {
			p.forceClosed = true
			close(p.forceCh)
		}
		var cbsList [][]func()
		var canceledFuts []*Future
		for _, it := range p.queue.drain() {
			p.canceled++
			canceledFuts = append(canceledFuts, it.fut)
			if c := it.fut.transition(StateCanceled, nil, ErrTaskCanceled, now, 0); c != nil {
				cbsList = append(cbsList, c)
			}
		}
		for id, e := range p.scheduled {
			e.timer.Stop()
			delete(p.scheduled, id)
			p.canceled++
			canceledFuts = append(canceledFuts, e.fut)
			if c := e.fut.transition(StateCanceled, nil, ErrTaskCanceled, now, 0); c != nil {
				cbsList = append(cbsList, c)
			}
		}
		p.wakeAllLocked()
		p.armWatcherLocked()
		p.mu.Unlock()
		// Snapshots take the future lock; build them only after releasing
		// the pool lock to preserve a consistent lock ordering.
		for _, f := range canceledFuts {
			notRun = append(notRun, f.Snapshot())
		}
		for _, c := range cbsList {
			fire(c)
		}
		p.emit(Event{Time: now, Kind: EventPoolForceCanceled, Pool: p.cfg.Name,
			Reason: "shutdown_now", Extra: map[string]any{"canceled": len(canceledFuts)}})
	} else {
		p.mu.Unlock()
	}
	return notRun, p.awaitDone(ctx)
}

// closeStopLocked closes stopCh once. Caller holds mu.
func (p *Pool) closeStopLocked() {
	if !p.stopClosed {
		p.stopClosed = true
		close(p.stopCh)
	}
}

// queueScheduledLocked stops every pending timer and immediately enqueues its
// task (graceful shutdown semantics). Caller holds mu.
func (p *Pool) queueScheduledLocked(now time.Time) {
	for id, e := range p.scheduled {
		e.timer.Stop()
		delete(p.scheduled, id)
		e.fut.transition(StateQueued, nil, nil, now, 0)
		p.queue.push(workItem{task: e.task, fut: e.fut})
	}
}

// AwaitTermination blocks until the pool terminates or ctx expires.
func (p *Pool) AwaitTermination(ctx context.Context) error { return p.awaitDone(ctx) }

// Terminated reports whether the pool has fully stopped.
func (p *Pool) Terminated() bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.status == statusTerminated
}

func (p *Pool) awaitDone(ctx context.Context) error {
	if ctx == nil {
		<-p.doneCh
		return nil
	}
	select {
	case <-p.doneCh:
		return nil
	case <-ctx.Done():
		return ErrShutdownTimeout
	}
}

func (p *Pool) armWatcherLocked() {
	if p.watchArmed {
		return
	}
	p.watchArmed = true
	go p.terminationWatcher()
}

// terminationWatcher waits for active == 0 with nothing left to run, then
// closes doneCh. If a graceful shutdown leaves queued work but no live worker
// (e.g. desired was 0), it spawns one internal drainer.
func (p *Pool) terminationWatcher() {
	for {
		p.mu.Lock()
		needDrainer := false
		done := false
		if p.status == statusTerminated {
			p.mu.Unlock()
			return
		}
		if p.status != statusRunning && p.active == 0 && p.spawning == 0 {
			switch {
			case p.queue.len() == 0 && len(p.scheduled) == 0:
				done = true
			case p.status == statusShuttingDown && p.queue.len() > 0:
				p.active++
				p.spawning++
				p.workersWG.Add(1)
				needDrainer = true
			}
		}
		if done {
			ch := p.doneCh
			now := p.clock.Now()
			completed := p.completed
			// Defer terminal bookkeeping until every worker goroutine has
			// fully returned (including emitting its worker.stopped event),
			// so the final events are observable before termination.
			p.mu.Unlock()
			go func() {
				p.workersWG.Wait()
				p.mu.Lock()
				p.status = statusTerminated
				p.desired = 0
				p.wakeAllLocked()
				p.mu.Unlock()
				p.emit(Event{Time: now, Kind: EventPoolTerminated, Pool: p.cfg.Name, Completed: completed})
				close(ch)
			}()
			return
		}
		// Drain any stale tick before parking, so a signal emitted after
		// this check is guaranteed to be observed on the channel.
		select {
		case <-p.watchCh:
		default:
		}
		p.mu.Unlock()
		if needDrainer {
			// Launch after unlock: the drainer's first Lock happens after
			// (and synchronizes with) this unlock.
			go p.drainer()
		}
		<-p.watchCh
	}
}

// ---------------------------------------------------------------- workers

// spawnLocked accounts for n new workers under the lock and returns their
// IDs. The caller launches the goroutines after Unlock(): launching under
// the lock would leave the child blocked on Lock() and therefore not yet
// registered as a cond waiter, so broadcasts emitted before it starts
// parking would be lost.
func (p *Pool) spawnLocked(n int) []int64 {
	ids := make([]int64, n)
	for i := 0; i < n; i++ {
		p.workerSeq++
		ids[i] = p.workerSeq
		p.active++
		p.spawning++
		p.workersWG.Add(1)
	}
	return ids
}

func (p *Pool) launchWorkers(ids []int64) {
	for _, id := range ids {
		id := id
		go p.worker(id)
	}
}

func (p *Pool) worker(id int64) {
	defer p.workersWG.Done()
	start := p.clock.Now()
	p.mu.Lock()
	p.spawning--
	cur := p.effectiveWorkers()
	desired := p.desired
	p.wakeAllLocked()
	p.mu.Unlock()
	p.emit(Event{Time: start, Kind: EventWorkerStarted, Pool: p.cfg.Name, WorkerID: id,
		Current: cur, Desired: desired})

	exit := func(reason string) {
		now := p.clock.Now()
		p.mu.Lock()
		p.active--
		cur := p.effectiveWorkers()
		ev := Event{Time: now, Kind: EventWorkerStopped, Pool: p.cfg.Name, WorkerID: id,
			Current: cur, Desired: p.desired, Reason: reason, QueueLen: p.queue.len()}
		p.wakeAllLocked()
		p.mu.Unlock()
		p.emit(ev)
	}
	// retire claims a shrink notice and removes the worker atomically in a
	// single critical section, so a concurrent Resize can never observe a
	// half-retired worker (notice claimed but active not yet decremented).
	retire := func() bool {
		now := p.clock.Now()
		p.mu.Lock()
		if p.retiring == 0 {
			p.mu.Unlock()
			return false
		}
		p.retiring--
		p.active--
		cur := p.effectiveWorkers()
		ev := Event{Time: now, Kind: EventWorkerStopped, Pool: p.cfg.Name, WorkerID: id,
			Current: cur, Desired: p.desired, Reason: "shrink", QueueLen: p.queue.len()}
		p.wakeAllLocked()
		p.mu.Unlock()
		p.emit(ev)
		return true
	}
	_ = exit

	for {
		// Force cancel: running tasks are interrupted only cooperatively via
		// ctx; queued work has already been drained, so exit as soon as we
		// are between tasks.
		select {
		case <-p.forceCh:
			exit("force_canceled")
			return
		default:
		}

		slot := make(chan struct{}, 1)
		var force, stop <-chan struct{}
		p.mu.Lock()
		if p.status == statusForce {
			p.mu.Unlock()
			exit("force_canceled")
			return
		}
		it, ok := p.queue.pop()
		if ok {
			p.started++
			p.running++
			cbs := it.fut.transition(StateRunning, nil, nil, p.clock.Now(), id)
			p.mu.Unlock()
			fire(cbs)
			p.emit(Event{Time: p.clock.Now(), Kind: EventTaskStarted, Pool: p.cfg.Name,
				TaskID: it.fut.id, WorkerID: id})
			p.runOne(id, it)
			continue
		}
		// No queued work. During graceful shutdown we exit now; outstanding
		// shrink notices are left behind and ignored by termination logic.
		if p.status == statusShuttingDown {
			p.mu.Unlock()
			exit("drained")
			return
		}
		// Idle: claim a shrink notice atomically with going away. Running
		// tasks never reach this point until fully complete, so a shrink can
		// never interrupt in-flight work.
		if p.retiring > 0 {
			p.mu.Unlock()
			if retire() {
				return
			}
			continue
		}
		// Register before parking and flush a stale tick. Any state change
		// that happens after this point either holds the lock and ticks our
		// slot, or closes stopCh/forceCh, so a wake-up cannot be lost.
		p.parked[id] = slot
		force = p.forceCh
		stop = p.stopCh
		p.mu.Unlock()

		select {
		case <-slot:
		case <-stop:
		case <-force:
			exit("force_canceled")
			return
		}

		p.mu.Lock()
		delete(p.parked, id)
		p.mu.Unlock()
	}
}

// drainer drains queued work during a graceful shutdown with zero workers.
func (p *Pool) drainer() {
	defer p.workersWG.Done()
	p.mu.Lock()
	p.spawning--
	id := int64(-2)
	p.wakeAllLocked()
	p.mu.Unlock()
	for {
		p.mu.Lock()
		if p.status == statusForce {
			p.active--
			p.wakeAllLocked()
			p.mu.Unlock()
			return
		}
		it, ok := p.queue.pop()
		if !ok {
			p.active--
			p.wakeAllLocked()
			p.mu.Unlock()
			return
		}
		p.started++
		p.running++
		cbs := it.fut.transition(StateRunning, nil, nil, p.clock.Now(), id)
		p.mu.Unlock()
		fire(cbs)
		p.runOne(id, it)
	}
}

func (p *Pool) runOne(workerID int64, it workItem) {
	res, err := p.executeOnce(chanCtx{parent: context.Background(), done: p.forceCh}, it.task)
	finish := p.clock.Now()

	p.mu.Lock()
	p.running--
	state := StateCompleted
	if err != nil {
		state = StateFailed
		p.failed++
	} else {
		p.completed++
	}
	cbs := it.fut.transition(state, res, err, finish, workerID)
	queueLen := p.queue.len()
	completed := p.completed
	p.wakeAllLocked()
	p.mu.Unlock()
	fire(cbs)
	kind := EventTaskCompleted
	if err != nil {
		kind = EventTaskFailed
	}
	p.emit(Event{Time: finish, Kind: kind, Pool: p.cfg.Name, TaskID: it.fut.id,
		WorkerID: workerID, QueueLen: queueLen, Completed: completed, Err: errStr(err)})
}

func (p *Pool) executeOnce(ctx context.Context, t Task) (res any, err error) {
	defer func() {
		if r := recover(); r != nil {
			err = fmt.Errorf("%w: %v", ErrTaskPanicked, r)
		}
	}()
	return p.executor.Execute(ctx, t.Fn)
}

// ---------------------------------------------------------------- introspection

// Stats returns a consistent snapshot of pool counters.
func (p *Pool) Stats() Stats {
	p.mu.Lock()
	defer p.mu.Unlock()
	return Stats{
		Name:            p.cfg.Name,
		State:           p.status.String(),
		DesiredWorkers:  p.desired,
		ActiveWorkers:   p.effectiveWorkers(),
		RetiringWorkers: p.retiring,
		QueueLen:        p.queue.len(),
		QueueCap:        p.cfg.QueueCapacity,
		AcceptDelayed:   len(p.scheduled),
		Accepted:        p.accepted,
		Rejected:        p.rejected,
		Started:         p.started,
		Completed:       p.completed,
		Failed:          p.failed,
		Canceled:        p.canceled,
		RunningNow:      p.running,
	}
}

// Task returns the snapshot of a known task and whether it exists.
func (p *Pool) Task(id string) (TaskSnapshot, bool) {
	p.mu.Lock()
	fut, ok := p.futures[id]
	p.mu.Unlock()
	if !ok {
		return TaskSnapshot{}, false
	}
	return fut.Snapshot(), true
}

// Tasks returns snapshots of all tasks known to the pool.
func (p *Pool) Tasks() []TaskSnapshot {
	p.mu.Lock()
	futs := make([]*Future, 0, len(p.futures))
	for _, f := range p.futures {
		futs = append(futs, f)
	}
	p.mu.Unlock()
	out := make([]TaskSnapshot, 0, len(futs))
	for _, f := range futs {
		out = append(out, f.Snapshot())
	}
	return out
}

// ---------------------------------------------------------------- helpers

func (p *Pool) genID() string {
	n := atomic.AddInt64(&p.taskIDSeq, 1)
	return "task-" + strconv.FormatInt(n, 10)
}

func (p *Pool) emit(e Event) { p.sink.Emit(e) }

// wakeAllLocked sends a non-blocking tick to every parked worker and to the
// termination watcher. Caller holds mu. Slots are capacity-1, so at most one
// tick per waiter is pending and the sends never block.
func (p *Pool) wakeAllLocked() {
	for _, ch := range p.parked {
		select {
		case ch <- struct{}{}:
		default:
		}
	}
	select {
	case p.watchCh <- struct{}{}:
	default:
	}
}

// fire runs settled callbacks after the pool lock has been released.
func fire(cbs []func()) {
	for _, cb := range cbs {
		func() {
			defer func() { _ = recover() }()
			cb()
		}()
	}
}

func errStr(err error) string {
	if err == nil {
		return ""
	}
	return err.Error()
}
