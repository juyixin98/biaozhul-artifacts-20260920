package scheduler

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"sync"
	"time"
)

// Sink receives structured events after the engine has committed each
// transition. Sinks are invoked from the engine goroutine, one event at a
// time; keep them fast and non-blocking (a channel-based sink with a small
// buffer is typical). The engine's own in-memory event log exists
// independently of sinks.
type Sink func(Event)

// Option configures an Engine.
type Option func(*Engine)

// WithClock replaces the wall clock (used in tests for deterministic retry
// backoff).
func WithClock(c Clock) Option {
	return func(e *Engine) { e.clock = c }
}

// WithSink appends an external event sink. Only set this at construction;
// sinks must not be added once the engine runs.
func WithSink(s Sink) Option {
	return func(e *Engine) { e.sinks = append(e.sinks, s) }
}

// WithMaxEventLog caps the in-memory per-run event log (default 10000).
func WithMaxEventLog(n int) Option {
	return func(e *Engine) {
		if n > 0 {
			e.maxEvents = n
		}
	}
}

// WithIDGenerator replaces run-id generation (used for deterministic tests).
func WithIDGenerator(g func() string) Option {
	return func(e *Engine) {
		if g != nil {
			e.genID = g
		}
	}
}

// Engine is an in-memory DAG scheduler. The zero value is not usable; create
// one with New.
//
// Concurrency model: a single engine goroutine owns every Run/Node state
// transition. Executors run on their own goroutines and report back through a
// channel; an attempt body therefore never mutates engine state. A node is
// launched only while it is in status "pending" and immediately flips to
// "running", which structurally guarantees that the same node can never have
// two attempts in flight concurrently.
type Engine struct {
	registry  *Registry
	clock     Clock
	sinks     []Sink
	maxEvents int
	genID     func() string

	// mu guards the runs map and every runState/nodeState field plus seq.
	// All mutating transitions run on the engine goroutine while holding the
	// write lock; Get/List/Events take the read lock.
	mu       sync.RWMutex
	runs     map[string]*runState
	runOrder []string
	seq      int64

	submitCh chan submitReq
	resultCh chan attemptResult
	retryCh  chan retryFire
	cancelCh chan cancelReq
	getCh    chan getReq
	listCh   chan listReq
	eventsCh chan eventsReq
	closeCh  chan struct{}
	closed   bool
	closeMu  sync.Mutex
	wg       sync.WaitGroup
}

type submitReq struct {
	dag    *DAG
	respCh chan submitResp
}

type submitResp struct {
	run *RunSnapshot
	err error
}

type attemptResult struct {
	runID   string
	nodeID  string
	attempt int
	err     error
}

type retryFire struct {
	runID  string
	nodeID string
	timer  Timer
}

type cancelReq struct {
	runID  string
	reason string
	respCh chan cancelResp
}

type cancelResp struct {
	run *RunSnapshot
	err error
}

type getReq struct {
	runID  string
	respCh chan getResp
}

type getResp struct {
	run *RunSnapshot
	err error
}

type listReq struct {
	respCh chan []*RunSnapshot
}

type eventsReq struct {
	runID  string
	respCh chan eventsResp
}

type eventsResp struct {
	events []Event
	err    error
}

// nodeState is the mutable per-node state. Touched only by the engine loop.
type nodeState struct {
	spec       NodeSpec
	status     NodeStatus
	attempts   int // attempts already launched
	inflight   bool
	waitTimer  Timer // active backoff timer while status == waiting
	startedAt  time.Time
	finishedAt time.Time
	lastError  string
	skipReason string
}

// runState is the mutable per-run state.
type runState struct {
	id           string
	dag          DAG
	nodes        map[string]*nodeState
	order        []string
	status       RunStatus
	createdAt    time.Time
	startedAt    time.Time
	finishedAt   time.Time
	canceled     bool
	cancelReason string
	ctx          context.Context
	cancelFn     context.CancelFunc
	events       []Event
	inflight     int
}

// NodeSnapshot is an immutable view of a node returned to clients.
type NodeSnapshot struct {
	ID          string            `json:"id"`
	TaskType    string            `json:"task_type"`
	Params      map[string]string `json:"params,omitempty"`
	DependsOn   []string          `json:"depends_on"`
	Policy      DependencyPolicy  `json:"policy"`
	MaxAttempts int               `json:"max_attempts"`
	Backoff     time.Duration     `json:"backoff_ns,omitempty"`
	Status      NodeStatus        `json:"status"`
	Attempts    int               `json:"attempts"`
	StartedAt   *time.Time        `json:"started_at,omitempty"`
	FinishedAt  *time.Time        `json:"finished_at,omitempty"`
	LastError   string            `json:"last_error,omitempty"`
	SkipReason  string            `json:"skip_reason,omitempty"`
}

// RunSnapshot is an immutable view of a run.
type RunSnapshot struct {
	ID           string                  `json:"id"`
	DAGID        string                  `json:"dag_id,omitempty"`
	Status       RunStatus               `json:"status"`
	CreatedAt    time.Time               `json:"created_at"`
	StartedAt    *time.Time              `json:"started_at,omitempty"`
	FinishedAt   *time.Time              `json:"finished_at,omitempty"`
	CancelReason string                  `json:"cancel_reason,omitempty"`
	Nodes        map[string]NodeSnapshot `json:"nodes"`
	NodeOrder    []string                `json:"node_order"`
}

// New builds an engine. The registry must contain every task_type referenced
// by submitted DAGs.
func New(registry *Registry, opts ...Option) *Engine {
	e := &Engine{
		registry:  registry,
		clock:     RealClock{},
		maxEvents: 10000,
		genID:     defaultID,
		runs:      map[string]*runState{},
		submitCh:  make(chan submitReq, 16),
		resultCh:  make(chan attemptResult, 64),
		retryCh:   make(chan retryFire, 64),
		cancelCh:  make(chan cancelReq, 16),
		getCh:     make(chan getReq, 32),
		listCh:    make(chan listReq, 8),
		eventsCh:  make(chan eventsReq, 16),
		closeCh:   make(chan struct{}),
	}
	for _, o := range opts {
		o(e)
	}
	e.wg.Add(1)
	go e.loop()
	return e
}

func defaultID() string {
	var b [8]byte
	_, _ = rand.Read(b[:])
	return "run_" + hex.EncodeToString(b[:])
}

// Close stops the engine loop and cancels all in-flight runs. Safe to call
// multiple times. Attempts that ignore their context may keep running in the
// background, but their results are discarded.
func (e *Engine) Close() {
	e.closeMu.Lock()
	if e.closed {
		e.closeMu.Unlock()
		return
	}
	e.closed = true
	close(e.closeCh)
	e.closeMu.Unlock()
	e.wg.Wait()
}

// Submit validates the DAG (cycle detection, references, task-type lookup)
// and starts the run, returning its first snapshot.
func (e *Engine) Submit(dag DAG) (*RunSnapshot, error) {
	if err := dag.Validate(); err != nil {
		return nil, err
	}
	for _, n := range dag.Nodes {
		if !e.registry.Has(n.TaskType) {
			return nil, fmt.Errorf("node %q: unknown task_type %q", n.ID, n.TaskType)
		}
	}
	respCh := make(chan submitResp, 1)
	select {
	case e.submitCh <- submitReq{dag: &dag, respCh: respCh}:
	case <-e.closeCh:
		return nil, errors.New("engine closed")
	}
	select {
	case r := <-respCh:
		return r.run, r.err
	case <-e.closeCh:
		return nil, errors.New("engine closed")
	}
}

// Get returns a snapshot of a run.
func (e *Engine) Get(runID string) (*RunSnapshot, error) {
	respCh := make(chan getResp, 1)
	select {
	case e.getCh <- getReq{runID: runID, respCh: respCh}:
	case <-e.closeCh:
		return e.getLocked(runID)
	}
	select {
	case r := <-respCh:
		return r.run, r.err
	case <-e.closeCh:
		return e.getLocked(runID)
	}
}

func (e *Engine) getLocked(runID string) (*RunSnapshot, error) {
	e.mu.RLock()
	defer e.mu.RUnlock()
	rs, ok := e.runs[runID]
	if !ok {
		return nil, ErrNotFound
	}
	return e.snapshotLocked(rs), nil
}

// List returns snapshots of all runs in submission order.
func (e *Engine) List() []*RunSnapshot {
	respCh := make(chan []*RunSnapshot, 1)
	select {
	case e.listCh <- listReq{respCh: respCh}:
		return <-respCh
	case <-e.closeCh:
		e.mu.RLock()
		defer e.mu.RUnlock()
		out := make([]*RunSnapshot, 0, len(e.runOrder))
		for _, id := range e.runOrder {
			out = append(out, e.snapshotLocked(e.runs[id]))
		}
		return out
	}
}

// Events returns the structured event log for one run, in emission order.
func (e *Engine) Events(runID string) ([]Event, error) {
	respCh := make(chan eventsResp, 1)
	select {
	case e.eventsCh <- eventsReq{runID: runID, respCh: respCh}:
	case <-e.closeCh:
		e.mu.RLock()
		defer e.mu.RUnlock()
		rs, ok := e.runs[runID]
		if !ok {
			return nil, ErrNotFound
		}
		return append([]Event(nil), rs.events...), nil
	}
	r := <-respCh
	return r.events, r.err
}

// Cancel requests cancellation of a run. In-flight attempts receive a
// canceled context; nodes waiting on backoff have their timers stopped; every
// node that has not run is marked canceled. Returns the snapshot at request
// time; use Wait/polling for the terminal state.
func (e *Engine) Cancel(runID, reason string) (*RunSnapshot, error) {
	respCh := make(chan cancelResp, 1)
	select {
	case e.cancelCh <- cancelReq{runID: runID, reason: reason, respCh: respCh}:
	case <-e.closeCh:
		return nil, errors.New("engine closed")
	}
	r := <-respCh
	return r.run, r.err
}

// Wait blocks until the run reaches a terminal state or ctx is done.
func (e *Engine) Wait(ctx context.Context, runID string) (*RunSnapshot, error) {
	ticker := time.NewTicker(2 * time.Millisecond)
	defer ticker.Stop()
	for {
		snap, err := e.Get(runID)
		if err != nil {
			return nil, err
		}
		if isRunTerminal(snap.Status) {
			return snap, nil
		}
		select {
		case <-ctx.Done():
			return snap, ctx.Err()
		case <-ticker.C:
		}
	}
}

func isRunTerminal(s RunStatus) bool {
	return s == RunSucceeded || s == RunFailed || s == RunCanceled
}

// ---- engine loop: sole owner of run state ----

func (e *Engine) loop() {
	defer e.wg.Done()
	for {
		select {
		case <-e.closeCh:
			e.shutdown()
			return
		case req := <-e.submitCh:
			req.respCh <- e.handleSubmit(req.dag)
		case res := <-e.resultCh:
			e.handleResult(res)
		case rf := <-e.retryCh:
			e.handleRetry(rf)
		case req := <-e.cancelCh:
			req.respCh <- e.handleCancel(req)
		case req := <-e.getCh:
			e.mu.RLock()
			rs, ok := e.runs[req.runID]
			var snap *RunSnapshot
			if ok {
				snap = e.snapshotLocked(rs)
			}
			e.mu.RUnlock()
			if ok {
				req.respCh <- getResp{run: snap}
			} else {
				req.respCh <- getResp{err: ErrNotFound}
			}
		case req := <-e.listCh:
			e.mu.RLock()
			out := make([]*RunSnapshot, 0, len(e.runOrder))
			for _, id := range e.runOrder {
				out = append(out, e.snapshotLocked(e.runs[id]))
			}
			e.mu.RUnlock()
			req.respCh <- out
		case req := <-e.eventsCh:
			e.mu.RLock()
			rs, ok := e.runs[req.runID]
			var ev []Event
			if ok {
				ev = append([]Event(nil), rs.events...)
			}
			e.mu.RUnlock()
			if ok {
				req.respCh <- eventsResp{events: ev}
			} else {
				req.respCh <- eventsResp{err: ErrNotFound}
			}
		}
	}
}

func (e *Engine) shutdown() {
	var emitted []Event
	e.mu.Lock()
	for _, rs := range e.runs {
		if isRunTerminal(rs.status) {
			continue
		}
		rs.canceled = true
		rs.cancelReason = "engine closed"
		rs.cancelFn()
		// Settle every node synchronously: waiting/pending nodes are
		// canceled outright; in-flight attempts have had their ctx
		// canceled and are marked canceled too (their results are
		// discarded once the loop exits).
		for _, ns := range rs.nodes {
			if ns.waitTimer != nil {
				ns.waitTimer.Stop()
				ns.waitTimer = nil
			}
			if !ns.status.terminal() {
				emitted = append(emitted, e.finishNodeLocked(rs, ns, StatusCanceled,
					"engine closed", nil))
			}
		}
		rs.finishedAt = e.clock.Now()
		rs.status = RunCanceled
		emitted = append(emitted, e.emitLocked(rs, Event{
			Type:      EventRunCanceled,
			RunStatus: RunCanceled,
			Reason:    "engine closed",
		}))
	}
	e.mu.Unlock()
	e.fanout(emitted)
}

func (e *Engine) handleSubmit(dag *DAG) submitResp {
	id := e.genID()
	ctx, cancelFn := context.WithCancel(context.Background())
	now := e.clock.Now()
	rs := &runState{
		id:        id,
		dag:       *dag,
		nodes:     make(map[string]*nodeState, len(dag.Nodes)),
		status:    RunPending,
		createdAt: now,
		ctx:       ctx,
		cancelFn:  cancelFn,
	}
	var emitted []Event
	e.mu.Lock()
	for _, n := range dag.Nodes {
		rs.order = append(rs.order, n.ID)
		rs.nodes[n.ID] = &nodeState{spec: n, status: StatusPending}
	}
	e.runs[id] = rs
	e.runOrder = append(e.runOrder, id)
	emitted = append(emitted, e.emitLocked(rs, Event{
		Type:      EventRunCreated,
		RunStatus: RunPending,
	}))
	rs.status = RunRunning
	rs.startedAt = now
	emitted = append(emitted, e.emitLocked(rs, Event{
		Type:      EventRunStarted,
		RunStatus: RunRunning,
	}))
	for _, nid := range rs.order {
		emitted = append(emitted, e.emitLocked(rs, Event{
			Type:   EventNodeQueued,
			NodeID: nid,
			Status: StatusPending,
		}))
	}
	e.pumpLocked(rs, &emitted)
	snap := e.snapshotLocked(rs)
	e.mu.Unlock()
	e.fanout(emitted)
	return submitResp{run: snap}
}

func (e *Engine) handleCancel(req cancelReq) cancelResp {
	var emitted []Event
	e.mu.Lock()
	rs, ok := e.runs[req.runID]
	if !ok {
		e.mu.Unlock()
		return cancelResp{err: ErrNotFound}
	}
	if isRunTerminal(rs.status) {
		snap := e.snapshotLocked(rs)
		e.mu.Unlock()
		return cancelResp{run: snap, err: ErrRunFinished}
	}
	rs.canceled = true
	rs.cancelReason = req.reason
	rs.cancelFn()
	emitted = append(emitted, e.emitLocked(rs, Event{
		Type:      EventRunCanceled,
		RunStatus: rs.status,
		Reason:    req.reason,
	}))
	// Nodes sleeping through backoff must be woken now, not at timer expiry.
	for _, ns := range rs.nodes {
		if ns.status == StatusWaiting && ns.waitTimer != nil {
			ns.waitTimer.Stop()
			ns.waitTimer = nil
			emitted = append(emitted, e.finishNodeLocked(rs, ns, StatusCanceled,
				"run canceled during retry backoff", nil))
		}
	}
	e.pumpLocked(rs, &emitted)
	snap := e.snapshotLocked(rs)
	e.mu.Unlock()
	e.fanout(emitted)
	return cancelResp{run: snap}
}

// pumpLocked makes every transition currently possible: propagates
// skip/cancel through resolved dependencies, launches ready nodes, and
// finishes the run once all nodes are terminal. Caller holds e.mu.
func (e *Engine) pumpLocked(rs *runState, emitted *[]Event) {
	// Fixed-point propagation: terminal deps can skip/cancel pending nodes,
	// whose new status then unlocks the next layer.
	for {
		changed := false
		for _, id := range rs.order {
			ns := rs.nodes[id]
			if ns.status != StatusPending {
				continue
			}
			decision, reason := evaluateDeps(rs, ns)
			switch decision {
			case depSkip:
				*emitted = append(*emitted, e.finishNodeLocked(rs, ns, StatusSkipped, reason, nil))
				changed = true
			case depCancel:
				*emitted = append(*emitted, e.finishNodeLocked(rs, ns, StatusCanceled, reason, nil))
				changed = true
			}
		}
		if !changed {
			break
		}
	}

	// Launch everything pending whose dependencies are satisfied. A node is
	// launchable only from status pending, which flips to running before the
	// attempt goroutine starts — duplicate concurrent launches are therefore
	// impossible by construction.
	for _, id := range rs.order {
		ns := rs.nodes[id]
		if ns.status != StatusPending {
			continue
		}
		if decision, _ := evaluateDeps(rs, ns); decision == depReady {
			e.launchLocked(rs, ns, emitted)
		}
	}

	e.maybeFinishLocked(rs, emitted)
}

type depDecision int

const (
	depWait   depDecision = iota // some deps unresolved
	depReady                     // policy satisfied, can run
	depSkip                      // must be skipped (failed/skipped upstream, all_success)
	depCancel                    // must be canceled (run canceled or canceled upstream)
)

func evaluateDeps(rs *runState, ns *nodeState) (depDecision, string) {
	if rs.canceled {
		return depCancel, "run canceled before node started"
	}
	if len(ns.spec.DependsOn) == 0 {
		return depReady, ""
	}
	unresolved := false
	sawFailed := false
	sawSkipped := false
	sawCanceled := false
	for _, dep := range ns.spec.DependsOn {
		switch rs.nodes[dep].status {
		case StatusFailed:
			sawFailed = true
		case StatusSkipped:
			sawSkipped = true
		case StatusCanceled:
			sawCanceled = true
		default:
			if !rs.nodes[dep].status.terminal() {
				unresolved = true
			}
		}
	}
	// A canceled upstream poisons the whole downstream chain with canceled.
	if sawCanceled {
		return depCancel, fmt.Sprintf("dependency %q was canceled",
			firstDepWith(rs, ns, StatusCanceled))
	}
	switch ns.spec.Policy {
	case PolicyAllFinished:
		if unresolved {
			return depWait, ""
		}
		return depReady, ""
	default: // PolicyAllSuccess
		if sawFailed || sawSkipped {
			bad := firstDepWith(rs, ns, StatusFailed)
			if bad == "" {
				bad = firstDepWith(rs, ns, StatusSkipped)
			}
			return depSkip, fmt.Sprintf(
				"dependency %q did not succeed (policy=all_success)", bad)
		}
		if unresolved {
			return depWait, ""
		}
		return depReady, ""
	}
}

func firstDepWith(rs *runState, ns *nodeState, want NodeStatus) string {
	for _, dep := range ns.spec.DependsOn {
		if rs.nodes[dep].status == want {
			return dep
		}
	}
	return ""
}

func (e *Engine) launchLocked(rs *runState, ns *nodeState, emitted *[]Event) {
	ns.attempts++
	ns.status = StatusRunning
	ns.inflight = true
	ns.startedAt = e.clock.Now()
	rs.inflight++
	attempt := ns.attempts

	depStatus := map[string]NodeStatus{}
	for _, dep := range ns.spec.DependsOn {
		depStatus[dep] = rs.nodes[dep].status
	}
	evType := EventNodeStarted
	if attempt > 1 {
		evType = EventNodeRetrying
	}
	*emitted = append(*emitted, e.emitLocked(rs, Event{
		Type:             evType,
		NodeID:           ns.spec.ID,
		Attempt:          attempt,
		MaxAttempts:      ns.spec.MaxAttempts,
		Status:           StatusRunning,
		DependencyStatus: depStatus,
	}))

	ex, _ := e.registry.Lookup(ns.spec.TaskType)
	in := ExecuteInput{
		RunID:    rs.id,
		NodeID:   ns.spec.ID,
		TaskType: ns.spec.TaskType,
		Params:   ns.spec.Params,
		Attempt:  attempt,
	}
	go func() {
		err := ex.Execute(rs.ctx, in)
		select {
		case e.resultCh <- attemptResult{
			runID:   rs.id,
			nodeID:  ns.spec.ID,
			attempt: attempt,
			err:     err,
		}:
		case <-e.closeCh:
		}
	}()
}

func (e *Engine) handleResult(res attemptResult) {
	var emitted []Event
	e.mu.Lock()
	rs, ok := e.runs[res.runID]
	if !ok {
		e.mu.Unlock()
		return
	}
	ns := rs.nodes[res.nodeID]
	// Stale guard: only the currently in-flight attempt drives transitions.
	if ns == nil || !ns.inflight || ns.attempts != res.attempt || ns.status != StatusRunning {
		e.mu.Unlock()
		return
	}
	ns.inflight = false
	rs.inflight--
	ns.finishedAt = e.clock.Now()
	if res.err != nil {
		ns.lastError = res.err.Error()
	}

	// Cancellation wins over whatever the attempt returned.
	if rs.canceled {
		emitted = append(emitted, e.finishNodeLocked(rs, ns, StatusCanceled,
			"run canceled while node was running", nil))
		e.pumpLocked(rs, &emitted)
		e.mu.Unlock()
		e.fanout(emitted)
		return
	}

	if res.err == nil {
		emitted = append(emitted, e.finishNodeLocked(rs, ns, StatusSucceeded, "", nil))
		e.pumpLocked(rs, &emitted)
		e.mu.Unlock()
		e.fanout(emitted)
		return
	}

	// Attempt failed: spend the retry budget or fail the node.
	if res.attempt < ns.spec.MaxAttempts {
		backoff := ns.spec.Backoff.Duration
		ns.status = StatusWaiting
		emitted = append(emitted, e.emitLocked(rs, Event{
			Type:        EventNodeRetryWait,
			NodeID:      ns.spec.ID,
			Attempt:     res.attempt,
			MaxAttempts: ns.spec.MaxAttempts,
			Status:      StatusWaiting,
			Error:       res.err.Error(),
			Backoff:     backoff,
		}))
		timer := e.clock.NewTimer(backoff)
		ns.waitTimer = timer
		e.mu.Unlock()
		e.fanout(emitted)

		go func(runID, nodeID string, t Timer) {
			select {
			case <-t.C():
				select {
				case e.retryCh <- retryFire{runID: runID, nodeID: nodeID, timer: t}:
				case <-e.closeCh:
				case <-rs.ctx.Done():
				}
			case <-e.closeCh:
			case <-rs.ctx.Done():
			}
		}(rs.id, ns.spec.ID, timer)
		return
	}

	emitted = append(emitted, e.finishNodeLocked(rs, ns, StatusFailed, "", nil))
	e.pumpLocked(rs, &emitted)
	e.mu.Unlock()
	e.fanout(emitted)
}

func (e *Engine) handleRetry(rf retryFire) {
	var emitted []Event
	e.mu.Lock()
	rs, ok := e.runs[rf.runID]
	if !ok {
		e.mu.Unlock()
		return
	}
	ns := rs.nodes[rf.nodeID]
	if ns == nil || ns.status != StatusWaiting || ns.waitTimer != rf.timer {
		// Stale (e.g. timer stopped by Cancel). Drain and drop it.
		e.mu.Unlock()
		return
	}
	ns.waitTimer = nil
	if rs.canceled {
		emitted = append(emitted, e.finishNodeLocked(rs, ns, StatusCanceled,
			"run canceled during retry backoff", nil))
		e.pumpLocked(rs, &emitted)
		e.mu.Unlock()
		e.fanout(emitted)
		return
	}
	ns.status = StatusPending
	e.pumpLocked(rs, &emitted)
	e.mu.Unlock()
	e.fanout(emitted)
}

// finishNodeLocked records a terminal node transition. Caller holds e.mu.
func (e *Engine) finishNodeLocked(rs *runState, ns *nodeState, status NodeStatus, reason string, attemptErr error) Event {
	ns.status = status
	if ns.finishedAt.IsZero() {
		ns.finishedAt = e.clock.Now()
	}
	ns.skipReason = reason
	var evType EventType
	switch status {
	case StatusSucceeded:
		evType = EventNodeSucceeded
	case StatusFailed:
		evType = EventNodeFailed
	case StatusSkipped:
		evType = EventNodeSkipped
	case StatusCanceled:
		evType = EventNodeCanceled
	}
	ev := Event{
		Type:        evType,
		NodeID:      ns.spec.ID,
		Attempt:     ns.attempts,
		MaxAttempts: ns.spec.MaxAttempts,
		Status:      status,
		Reason:      reason,
	}
	if attemptErr != nil {
		ev.Error = attemptErr.Error()
	}
	return e.emitLocked(rs, ev)
}

func (e *Engine) maybeFinishLocked(rs *runState, emitted *[]Event) {
	if isRunTerminal(rs.status) {
		return
	}
	allTerminal := true
	anyFailed := false
	anyCanceled := false
	for _, id := range rs.order {
		s := rs.nodes[id].status
		if !s.terminal() {
			allTerminal = false
			break
		}
		switch s {
		case StatusFailed:
			anyFailed = true
		case StatusCanceled:
			anyCanceled = true
		}
	}
	if !allTerminal {
		return
	}
	rs.finishedAt = e.clock.Now()
	var final RunStatus
	var evType EventType
	switch {
	case anyFailed:
		final = RunFailed
		evType = EventRunFailed
	case rs.canceled || anyCanceled:
		final = RunCanceled
		evType = EventRunCanceled
	default:
		final = RunSucceeded
		evType = EventRunSucceeded
	}
	rs.status = final
	*emitted = append(*emitted, e.emitLocked(rs, Event{
		Type:      evType,
		RunStatus: final,
		Reason:    rs.cancelReason,
	}))
}

// emitLocked appends an event to the run log and stamps it. Caller holds e.mu.
// Sink fan-out happens later via fanout, outside the lock.
func (e *Engine) emitLocked(rs *runState, ev Event) Event {
	e.seq++
	ev.Seq = e.seq
	ev.RunID = rs.id
	if ev.Time.IsZero() {
		ev.Time = e.clock.Now()
	}
	rs.events = append(rs.events, ev)
	if e.maxEvents > 0 && len(rs.events) > e.maxEvents {
		rs.events = rs.events[len(rs.events)-e.maxEvents:]
	}
	return ev
}

func (e *Engine) fanout(events []Event) {
	if len(e.sinks) == 0 {
		return
	}
	for _, ev := range events {
		for _, s := range e.sinks {
			s(ev)
		}
	}
}

func (e *Engine) snapshotLocked(rs *runState) *RunSnapshot {
	nodes := make(map[string]NodeSnapshot, len(rs.nodes))
	for _, id := range rs.order {
		ns := rs.nodes[id]
		snap := NodeSnapshot{
			ID:          ns.spec.ID,
			TaskType:    ns.spec.TaskType,
			Params:      ns.spec.Params,
			DependsOn:   append([]string(nil), ns.spec.DependsOn...),
			Policy:      ns.spec.Policy,
			MaxAttempts: ns.spec.MaxAttempts,
			Backoff:     ns.spec.Backoff.Duration,
			Status:      ns.status,
			Attempts:    ns.attempts,
			LastError:   ns.lastError,
			SkipReason:  ns.skipReason,
		}
		if !ns.startedAt.IsZero() {
			t := ns.startedAt
			snap.StartedAt = &t
		}
		if !ns.finishedAt.IsZero() {
			t := ns.finishedAt
			snap.FinishedAt = &t
		}
		nodes[id] = snap
	}
	snap := &RunSnapshot{
		ID:           rs.id,
		DAGID:        rs.dag.ID,
		Status:       rs.status,
		CreatedAt:    rs.createdAt,
		CancelReason: rs.cancelReason,
		Nodes:        nodes,
		NodeOrder:    append([]string(nil), rs.order...),
	}
	if !rs.startedAt.IsZero() {
		t := rs.startedAt
		snap.StartedAt = &t
	}
	if !rs.finishedAt.IsZero() {
		t := rs.finishedAt
		snap.FinishedAt = &t
	}
	return snap
}
