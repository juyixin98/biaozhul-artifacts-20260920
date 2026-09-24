// Package scheduler runs DAG instances: dependency scheduling, bounded
// retries with exponential back-off, cooperative cancellation, result reuse
// (successful nodes are never re-executed within an instance) and crash
// recovery driven by the persisted state store.
package scheduler

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"log"
	"sync"
	"time"

	"dagexec/internal/dag"
	"dagexec/internal/store"
)

// Store is the persistence surface the scheduler needs (implemented by
// store.FileStore).
type Store interface {
	Create(*dag.DAGState) error
	Update(id string, fn func(*dag.DAGState) error) error
	Get(id string) (*dag.DAGState, error)
	List() ([]*dag.DAGState, error)
}

// Sentinel errors, mapped to HTTP status codes by the API layer.
var (
	ErrNotFound = errors.New("dag not found")
	ErrConflict = errors.New("operation not allowed in current dag status")
)

// Options configures a Scheduler.
type Options struct {
	DefaultMaxAttempts int           // per activation; 0 -> 1
	DefaultMaxParallel int           // per DAG; 0 -> 4
	RetryBaseDelay     time.Duration // first back-off; doubled each attempt; 0 -> 200ms
	MaxRetryDelay      time.Duration // cap; 0 -> 30s
	Logger             *log.Logger
}

// Scheduler owns the registry of active DAG runners and serializes
// scheduler-wide bookkeeping. Per-DAG state transitions happen inside the
// DAG's single runner goroutine (except Cancel/Retry requests, which only
// persist intent and signal that goroutine).
type Scheduler struct {
	store Store
	reg   map[string]dag.TaskFunc
	opts  Options
	logf  func(string, ...interface{})

	mu      sync.Mutex
	runners map[string]*runner
	wg      sync.WaitGroup

	rootCtx    context.Context
	rootCancel context.CancelFunc
}

// New creates a scheduler. Start must be called to resume DAGs found in the
// store.
func New(st Store, registry map[string]dag.TaskFunc, opts Options) *Scheduler {
	if opts.DefaultMaxAttempts <= 0 {
		opts.DefaultMaxAttempts = 1
	}
	if opts.DefaultMaxParallel <= 0 {
		opts.DefaultMaxParallel = 4
	}
	if opts.RetryBaseDelay <= 0 {
		opts.RetryBaseDelay = 200 * time.Millisecond
	}
	if opts.MaxRetryDelay <= 0 {
		opts.MaxRetryDelay = 30 * time.Second
	}
	logf := func(string, ...interface{}) {}
	if opts.Logger != nil {
		logf = opts.Logger.Printf
	}
	ctx, cancel := context.WithCancel(context.Background())
	return &Scheduler{
		store: st, reg: registry, opts: opts, logf: logf,
		runners:    map[string]*runner{},
		rootCtx:    ctx,
		rootCancel: cancel,
	}
}

// Start launches a runner for every DAG in the store. Runners are
// long-lived: a terminal DAG's runner merely parks until a Retry/Cancel
// wakes it, which keeps retry signalling race-free across the
// terminal->reactivated transition.
func (s *Scheduler) Start() error {
	list, err := s.store.List()
	if err != nil {
		return err
	}
	for _, st := range list {
		s.ensureRunner(st.ID)
	}
	return nil
}

// Shutdown stops all runners. In-flight task goroutines lose their driver;
// nodes persisted as "running" are reset to pending on the next Start.
func (s *Scheduler) Shutdown() {
	s.rootCancel()
	s.wg.Wait()
}

// Submit validates a spec, persists a fresh DAG and starts its runner.
func (s *Scheduler) Submit(spec dag.Spec) (*dag.DAGState, error) {
	if err := spec.Validate(s.reg); err != nil {
		return nil, err
	}
	// Freeze resolved defaults into the spec so the persisted DAG is
	// self-contained across restarts with different server flags.
	if spec.MaxAttempts <= 0 {
		spec.MaxAttempts = s.opts.DefaultMaxAttempts
	}
	if spec.MaxParallel <= 0 {
		spec.MaxParallel = s.opts.DefaultMaxParallel
	}
	id, err := newID()
	if err != nil {
		return nil, err
	}
	now := time.Now().UTC()
	st := &dag.DAGState{
		ID:        id,
		Name:      spec.Name,
		Spec:      spec,
		Nodes:     map[string]*dag.NodeState{},
		Status:    dag.DAGPending,
		CreatedAt: now,
		UpdatedAt: now,
	}
	for i := range spec.Nodes {
		st.Nodes[spec.Nodes[i].ID] = &dag.NodeState{Status: dag.StatusPending}
	}
	if err := s.store.Create(st); err != nil {
		return nil, err
	}
	s.logf("dag %s accepted: %q with %d nodes", id, spec.Name, len(spec.Nodes))
	s.ensureRunner(id)
	return s.store.Get(id)
}

// Get returns a deep copy of one DAG.
func (s *Scheduler) Get(id string) (*dag.DAGState, error) {
	st, err := s.store.Get(id)
	if errors.Is(err, store.ErrNotFound) {
		return nil, ErrNotFound
	}
	return st, err
}

// List returns all DAG states (deep copies).
func (s *Scheduler) List() ([]*dag.DAGState, error) { return s.store.List() }

// Cancel requests cancellation. The request is persisted (status
// "cancelling") before active tasks are interrupted, so a crash in between
// still leads to cancellation on restart.
func (s *Scheduler) Cancel(id string) error {
	st, err := s.store.Get(id)
	if err != nil {
		return mapGetErr(err)
	}
	switch st.Status {
	case dag.DAGPending, dag.DAGRunning, dag.DAGFailed:
	default:
		return fmt.Errorf("%w: dag is %s", ErrConflict, st.Status)
	}
	r := s.ensureRunner(id)
	r.opMu.Lock()
	defer r.opMu.Unlock()

	// Re-check under the lock: a concurrent Retry may have changed the
	// status (in which case cancellation of that new activation is still
	// permitted), or completed it to succeeded/cancelled.
	st, err = s.store.Get(id)
	if err != nil {
		return mapGetErr(err)
	}
	switch st.Status {
	case dag.DAGPending, dag.DAGRunning, dag.DAGFailed, dag.DAGCancelling:
	default:
		return fmt.Errorf("%w: dag is %s", ErrConflict, st.Status)
	}
	if err := s.store.Update(id, func(st *dag.DAGState) error {
		st.Status = dag.DAGCancelling
		return nil
	}); err != nil {
		return err
	}
	r.signalCancel()
	s.logf("dag %s: cancellation requested", id)
	return nil
}

// Retry reactivates a failed or cancelled DAG. Successful nodes and their
// results are kept; failed/blocked/cancelled nodes are reset. TotalRuns is
// deliberately not reset: it records the real number of executions.
func (s *Scheduler) Retry(id string) error {
	st, err := s.store.Get(id)
	if err != nil {
		return mapGetErr(err)
	}
	switch st.Status {
	case dag.DAGFailed, dag.DAGCancelled:
	default:
		return fmt.Errorf("%w: retry is only valid for failed/cancelled dags, current status %q", ErrConflict, st.Status)
	}
	r := s.ensureRunner(id)
	r.opMu.Lock()
	defer r.opMu.Unlock()

	// Re-check under the lock: a concurrent Cancel may have moved the DAG
	// into cancelling/cancelled, in which case this retry must not race it.
	st, err = s.store.Get(id)
	if err != nil {
		return mapGetErr(err)
	}
	switch st.Status {
	case dag.DAGFailed, dag.DAGCancelled:
	default:
		return fmt.Errorf("%w: retry is only valid for failed/cancelled dags, current status %q", ErrConflict, st.Status)
	}
	if err := s.store.Update(id, func(st *dag.DAGState) error {
		for _, ns := range st.Nodes {
			switch ns.Status {
			case dag.StatusFailed, dag.StatusBlocked, dag.StatusCancelled, dag.StatusRunning:
				ns.Status = dag.StatusPending
				ns.Attempt = 0
				ns.Error = ""
				ns.Result = nil
				ns.RetryAfter = nil
				ns.StartedAt = nil
				ns.FinishedAt = nil
			}
		}
		st.FailNode = ""
		st.Status = dag.DAGRunning
		return nil
	}); err != nil {
		return err
	}
	s.logf("dag %s: retry requested", id)
	r.resetCancel()
	r.wake()
	return nil
}

// Wait blocks until the DAG reaches a terminal status, ctx is done, or the
// timeout elapses. Terminal means: succeeded, fully cancelled, or failed
// with nothing left that could run.
func (s *Scheduler) Wait(ctx context.Context, id string, timeout time.Duration) (*dag.DAGState, error) {
	deadline := time.Now().Add(timeout)
	for {
		st, err := s.store.Get(id)
		if err != nil {
			return nil, mapGetErr(err)
		}
		if IsTerminal(st) {
			return st, nil
		}
		if time.Now().After(deadline) {
			return st, context.DeadlineExceeded
		}
		select {
		case <-ctx.Done():
			return st, ctx.Err()
		case <-time.After(10 * time.Millisecond):
		}
	}
}

// IsTerminal reports whether no further node can ever run without a Retry.
func IsTerminal(st *dag.DAGState) bool {
	switch st.Status {
	case dag.DAGSucceeded, dag.DAGCancelled:
		return true
	}
	for _, ns := range st.Nodes {
		if ns.Status == dag.StatusRunning {
			return false
		}
	}
	if st.Status == dag.DAGFailed {
		// Pending nodes outside the failure fan-out can still run; a failed
		// node within its attempt budget will retry after back-off.
		doomed := failureFanOut(st)
		for id, ns := range st.Nodes {
			if ns.Status == dag.StatusPending && !doomed[id] {
				return false
			}
			if ns.Status == dag.StatusFailed && ns.Attempt < st.Spec.MaxAttemptsFor(nodeSpec(st, id), 1) {
				return false
			}
		}
		return true
	}
	return false
}

// ---------------------------------------------------------------- runners

type taskResult struct {
	nodeID string
	value  interface{}
	err    error
}

// activeCancel pairs a node's cancel func with a comparable token so stale
// goroutines from an earlier activation do not delete a newer registration.
type activeCancel struct {
	cancel context.CancelFunc
	token  *struct{}
}

type runner struct {
	s        *Scheduler
	id       string
	sem      chan struct{}
	resultCh chan taskResult
	wakeCh   chan struct{}

	// opMu serializes external control operations (Cancel/Retry) against
	// this DAG, so a Retry and a Cancel cannot interleave their state
	// transitions.
	opMu sync.Mutex

	mu              sync.Mutex
	cancelCh        chan struct{} // replaceable; replaced on reactivation
	cancelRequested bool
	cancels         map[string]activeCancel // active node contexts
}

func (s *Scheduler) ensureRunner(id string) *runner {
	s.mu.Lock()
	defer s.mu.Unlock()
	if r, ok := s.runners[id]; ok {
		return r
	}
	st, err := s.store.Get(id)
	if err != nil {
		return nil
	}
	parallel := st.Spec.MaxParallelFor(s.opts.DefaultMaxParallel)
	r := &runner{
		s:        s,
		id:       id,
		sem:      make(chan struct{}, parallel),
		resultCh: make(chan taskResult, len(st.Spec.Nodes)),
		cancelCh: make(chan struct{}),
		wakeCh:   make(chan struct{}, 1),
		cancels:  map[string]activeCancel{},
	}
	s.runners[id] = r
	s.wg.Add(1)
	go func() {
		defer s.wg.Done()
		r.run(s.rootCtx)
	}()
	return r
}

func (r *runner) wake() {
	select {
	case r.wakeCh <- struct{}{}:
	default:
	}
}

func (r *runner) signalCancel() {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.cancelRequested = true
	select {
	case <-r.cancelCh:
		// already closed
	default:
		close(r.cancelCh)
	}
}

// resetCancel arms a fresh cancellation channel for a new activation
// (after Retry reactivates a previously cancelled DAG).
func (r *runner) resetCancel() {
	r.mu.Lock()
	defer r.mu.Unlock()
	if !r.cancelRequested {
		return
	}
	r.cancelRequested = false
	r.cancelCh = make(chan struct{})
}

func (r *runner) run(parent context.Context) {
	ctx, cancel := context.WithCancel(parent)
	defer cancel()

	// Crash recovery before the first scheduling pass.
	if err := r.recover(); err != nil {
		r.s.logf("dag %s: recovery error: %v", r.id, err)
	}
	r.tick(ctx)

	for {
		st, err := r.s.store.Get(r.id)
		if err != nil {
			r.s.logf("dag %s: state read error: %v", r.id, err)
			return
		}
		r.mu.Lock()
		cancelCh := r.cancelCh
		r.mu.Unlock()
		if IsTerminal(st) {
			// Park instead of exiting: Retry/Cancel can arrive at any
			// moment and must not race with runner teardown. A parked
			// terminal DAG has no due back-off timer. Only a "failed" DAG
			// can still receive a meaningful Cancel; succeeded/cancelled
			// DAGs ignore the (already-closed) cancel channel so the loop
			// does not spin on it.
			var parkedCancel <-chan struct{}
			if st.Status == dag.DAGFailed {
				parkedCancel = cancelCh
			}
			select {
			case <-parent.Done():
				return
			case <-parkedCancel:
				r.applyCancelSweep()
			case <-r.wakeCh:
				// Retry reactivated the DAG.
			}
			r.tick(ctx)
			continue
		}

		var timerC <-chan time.Time
		timer := r.nextBackoffTimer(st)
		if timer != nil {
			timerC = timer.C
		}
		select {
		case <-parent.Done():
			if timer != nil {
				timer.Stop()
			}
			return
		case <-cancelCh:
			if timer != nil {
				timer.Stop()
			}
			r.applyCancelSweep()
		case res := <-r.resultCh:
			if timer != nil {
				timer.Stop()
			}
			r.applyResult(res)
		case <-timerC:
			// back-off elapsed; tick relaunches due nodes
		case <-r.wakeCh:
			if timer != nil {
				timer.Stop()
			}
		}
		r.tick(ctx)
	}
}

// recover normalizes state left by a crashed process.
func (r *runner) recover() error {
	return r.s.store.Update(r.id, func(st *dag.DAGState) error {
		if st.Status == dag.DAGCancelling {
			// Process died while cancelling: conservatively cancel everything
			// that did not finish successfully.
			for _, ns := range st.Nodes {
				if ns.Status != dag.StatusSuccess {
					ns.Status = dag.StatusCancelled
					ns.RetryAfter = nil
					ns.Error = ""
				}
			}
			st.Status = dag.DAGCancelled
			return nil
		}
		// Nodes persisted as "running" died with the process; relaunch them.
		// But a terminal DAG must not be resurrected: the orphaned node is
		// reconciled to the DAG's terminal outcome instead.
		terminal := st.Status == dag.DAGSucceeded || st.Status == dag.DAGCancelled
		changed := false
		for _, ns := range st.Nodes {
			if ns.Status == dag.StatusRunning {
				if terminal {
					if st.Status == dag.DAGCancelled {
						ns.Status = dag.StatusCancelled
						ns.Error = ""
					} else {
						// Should not normally happen (a succeeded DAG has
						// no running nodes); keep it non-terminal so the
						// node can finish rather than silently dropping it.
						ns.Status = dag.StatusPending
						changed = true
					}
				} else {
					ns.Status = dag.StatusPending
					ns.StartedAt = nil
					changed = true
				}
			}
		}
		if !terminal && (st.Status == dag.DAGPending || changed) {
			st.Status = dag.DAGRunning
		}
		return nil
	})
}

// applyCancelSweep marks everything that is not finished or actively running
// as cancelled, then interrupts active tasks. New nodes are never launched in
// the cancelling state (tick refuses); tasks that already completed
// successfully before the interrupt keep their result. Idempotent: may be
// invoked repeatedly from cancel and tick.
func (r *runner) applyCancelSweep() {
	err := r.s.store.Update(r.id, func(st *dag.DAGState) error {
		st.Status = dag.DAGCancelling
		for _, ns := range st.Nodes {
			if ns.Status != dag.StatusSuccess && ns.Status != dag.StatusRunning {
				ns.Status = dag.StatusCancelled
				ns.RetryAfter = nil
				ns.Error = ""
			}
		}
		return nil
	})
	if err != nil {
		r.s.logf("dag %s: cancel persist error: %v", r.id, err)
	}
	r.mu.Lock()
	for _, ac := range r.cancels {
		ac.cancel()
	}
	r.mu.Unlock()
}

// applyResult records one finished attempt and either parks the node in
// back-off/exhausted-failure or caches its result.
func (r *runner) applyResult(res taskResult) {
	err := r.s.store.Update(r.id, func(st *dag.DAGState) error {
		ns := st.Nodes[res.nodeID]
		now := time.Now().UTC()
		ns.FinishedAt = &now
		if res.err != nil {
			ns.Error = res.err.Error()
			switch {
			case st.Status == dag.DAGCancelling:
				// Interrupted by the user's cancel (or failed while a cancel
				// was in flight): it will not run again in this instance.
				ns.Status = dag.StatusCancelled
				ns.RetryAfter = nil
			default:
				ns.Status = dag.StatusFailed
				maxN := st.Spec.MaxAttemptsFor(nodeSpec(st, res.nodeID), r.s.opts.DefaultMaxAttempts)
				if ns.Attempt >= maxN {
					ns.RetryAfter = nil
					if st.FailNode == "" {
						st.FailNode = res.nodeID
					}
					st.Status = dag.DAGFailed
					// Propagate "blocked" to every doomed descendant in the
					// same transaction so external observers never see a
					// terminal DAG whose downstream nodes are still pending.
					doomed := failureFanOut(st)
					for dID, dns := range st.Nodes {
						if dID == res.nodeID || !doomed[dID] {
							continue
						}
						if dns.Status == dag.StatusPending || dns.Status == dag.StatusFailed {
							dns.Status = dag.StatusBlocked
							dns.RetryAfter = nil
							dns.Error = fmt.Sprintf("upstream node %q failed permanently", st.FailNode)
						}
					}
					r.s.logf("dag %s: node %s exhausted %d attempt(s)", r.id, res.nodeID, maxN)
				} else {
					d := r.backoff(ns.Attempt)
					t := time.Now().UTC().Add(d)
					ns.RetryAfter = &t
					r.s.logf("dag %s: node %s attempt %d failed (%v); retry after %s",
						r.id, res.nodeID, ns.Attempt, res.err, d)
				}
			}
			return nil
		}
		ns.Status = dag.StatusSuccess
		ns.Result = res.value
		ns.Error = ""
		ns.RetryAfter = nil
		r.s.logf("dag %s: node %s succeeded (lifetime run #%d)", r.id, res.nodeID, ns.TotalRuns)
		return nil
	})
	if err != nil {
		r.s.logf("dag %s: result persist error: %v", r.id, err)
	}
}

// tick performs one scheduling pass: propagate failure/cancellation, mark
// node transitions persistently, and launch everything the concurrency
// budget allows. All state mutations happen in one persisted update *before*
// any task goroutine starts, so a crash never leaves an untracked launch.
func (r *runner) tick(ctx context.Context) {
	type launch struct {
		id   string
		task string
		in   dag.Input
	}
	var launches []launch

	err := r.s.store.Update(r.id, func(st *dag.DAGState) error {
		now := time.Now().UTC()
		doomed := failureFanOut(st)
		cancelling := st.Status == dag.DAGCancelling

		for i := range st.Spec.Nodes {
			n := &st.Spec.Nodes[i]
			ns := st.Nodes[n.ID]

			if cancelling {
				// Nothing new ever launches once cancellation is requested;
				// running tasks are interrupted separately.
				if ns.Status != dag.StatusSuccess && ns.Status != dag.StatusRunning {
					ns.Status = dag.StatusCancelled
					ns.RetryAfter = nil
					ns.Error = ""
				}
				continue
			}

			switch ns.Status {
			case dag.StatusPending:
				if st.Status == dag.DAGFailed && doomed[n.ID] {
					ns.Status = dag.StatusBlocked
					ns.Error = fmt.Sprintf("upstream node %q failed permanently", st.FailNode)
					continue
				}
				state := r.depReadiness(st, n)
				switch state {
				case depBlocked:
					ns.Status = dag.StatusBlocked
				case depCancelled:
					ns.Status = dag.StatusCancelled
				case depWaiting:
					// stay pending
				case depReady:
					// launched below
				}
			case dag.StatusFailed:
				if doomed[n.ID] && n.ID != st.FailNode {
					ns.Status = dag.StatusBlocked
					ns.RetryAfter = nil
					continue
				}
				maxN := st.Spec.MaxAttemptsFor(*n, r.s.opts.DefaultMaxAttempts)
				if ns.Attempt >= maxN {
					continue // permanently failed; fan-out handles dependents
				}
				if ns.RetryAfter != nil && ns.RetryAfter.After(now) {
					continue // waiting out back-off
				}
				// due for retry; launched below
			}
		}

		// Completion / status bookkeeping.
		allSuccess := true
		hasRunning := false
		for i := range st.Spec.Nodes {
			switch st.Nodes[st.Spec.Nodes[i].ID].Status {
			case dag.StatusSuccess:
			default:
				allSuccess = false
			}
			if st.Nodes[st.Spec.Nodes[i].ID].Status == dag.StatusRunning {
				hasRunning = true
			}
		}
		switch {
		case allSuccess:
			st.Status = dag.DAGSucceeded
		case st.Status == dag.DAGCancelling && !hasRunning:
			st.Status = dag.DAGCancelled
		case st.Status == dag.DAGCancelled:
			// Terminal cancelled (e.g. recovered after a crash): must not
			// be resurrected to running even if some node is non-success.
			st.Status = dag.DAGCancelled
		case st.Status != dag.DAGFailed && st.Status != dag.DAGCancelling:
			st.Status = dag.DAGRunning
		}
		if st.Status == dag.DAGSucceeded || st.Status == dag.DAGCancelled {
			return nil
		}
		if st.Status == dag.DAGFailed && IsTerminal(st) {
			return nil
		}

		// Launch ready nodes in spec order while concurrency slots exist.
		for i := range st.Spec.Nodes {
			n := &st.Spec.Nodes[i]
			ns := st.Nodes[n.ID]
			runnable := ns.Status == dag.StatusPending && r.depReadiness(st, n) == depReady
			retryDue := ns.Status == dag.StatusFailed &&
				ns.Attempt < st.Spec.MaxAttemptsFor(*n, r.s.opts.DefaultMaxAttempts) &&
				(ns.RetryAfter == nil || !ns.RetryAfter.After(now)) &&
				!(st.Status == dag.DAGFailed && doomed[n.ID] && n.ID != st.FailNode)
			if !runnable && !retryDue {
				continue
			}
			select {
			case r.sem <- struct{}{}:
			default:
				continue // concurrency budget exhausted; next tick tries again
			}

			in := dag.Input{Params: n.Params, Upstream: map[string]interface{}{}, Attempt: ns.Attempt + 1, TotalAttempt: ns.TotalRuns + 1}
			for _, d := range n.Deps {
				in.Upstream[d] = st.Nodes[d].Result
			}
			ns.Status = dag.StatusRunning
			ns.Attempt++
			ns.TotalRuns++
			ns.Error = ""
			ns.RetryAfter = nil
			t := now
			ns.StartedAt = &t
			launches = append(launches, launch{id: n.ID, task: n.Task, in: in})
		}
		return nil
	})
	if err != nil {
		r.s.logf("dag %s: tick persist error: %v", r.id, err)
		return
	}

	// If a cancel landed during this pass (or was already pending), make
	// sure any still-active task is interrupted.
	if r.cancelWasRequested() {
		r.interruptActive()
	}

	// Spawn after the state is durable.
	for _, l := range launches {
		fn := r.s.reg[l.task]
		nodeCtx, nodeCancel := context.WithCancel(ctx)
		ac := activeCancel{cancel: nodeCancel, token: new(struct{})}
		r.mu.Lock()
		// Cancel may have landed in the tiny window before registration;
		// honour it immediately rather than leaking the goroutine.
		if r.cancelRequested {
			nodeCancel()
		}
		r.cancels[l.id] = ac
		r.mu.Unlock()
		go func(l launch, ac activeCancel) {
			defer func() { <-r.sem }()
			defer nodeCancel()
			defer func() {
				r.mu.Lock()
				// A new activation's node with the same id would register
				// a different token; delete only our registration.
				if cur := r.cancels[l.id]; cur.token == ac.token {
					delete(r.cancels, l.id)
				}
				r.mu.Unlock()
			}()
			val, err := fn(nodeCtx, l.in)
			select {
			case r.resultCh <- taskResult{nodeID: l.id, value: val, err: err}:
			case <-ctx.Done():
				// Server shutting down; "running" stays on disk and is reset
				// to pending during the next process recovery.
			}
		}(l, ac)
	}
}

func (r *runner) cancelWasRequested() bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.cancelRequested
}

func (r *runner) interruptActive() {
	r.mu.Lock()
	defer r.mu.Unlock()
	for _, ac := range r.cancels {
		ac.cancel()
	}
}

type depState int

const (
	depReady depState = iota
	depWaiting
	depBlocked
	depCancelled
)

func (r *runner) depReadiness(st *dag.DAGState, n *dag.NodeSpec) depState {
	sawBlock := false
	for _, d := range n.Deps {
		dn := st.Nodes[d]
		switch dn.Status {
		case dag.StatusSuccess:
		case dag.StatusBlocked:
			sawBlock = true
		case dag.StatusFailed:
			// Still inside its retry budget: wait. Only an exhausted
			// upstream failure blocks this node.
			maxN := st.Spec.MaxAttemptsFor(nodeSpec(st, d), 1)
			if dn.Attempt >= maxN {
				sawBlock = true
			} else {
				return depWaiting
			}
		case dag.StatusCancelled:
			return depCancelled
		default:
			return depWaiting
		}
	}
	if sawBlock {
		return depBlocked
	}
	return depReady
}

func (r *runner) nextBackoffTimer(st *dag.DAGState) *time.Timer {
	var earliest *time.Time
	now := time.Now()
	for _, ns := range st.Nodes {
		if ns.Status == dag.StatusFailed && ns.RetryAfter != nil {
			if earliest == nil || ns.RetryAfter.Before(*earliest) {
				t := *ns.RetryAfter
				earliest = &t
			}
		}
	}
	if earliest == nil {
		return nil
	}
	d := earliest.Sub(now)
	if d < 0 {
		d = 0
	}
	return time.NewTimer(d)
}

func (r *runner) backoff(attempt int) time.Duration {
	d := r.s.opts.RetryBaseDelay
	for i := 1; i < attempt; i++ {
		d *= 2
		if d >= r.s.opts.MaxRetryDelay {
			return r.s.opts.MaxRetryDelay
		}
	}
	if d > r.s.opts.MaxRetryDelay {
		return r.s.opts.MaxRetryDelay
	}
	return d
}

// ---------------------------------------------------------------- helpers

// failureFanOut returns the set of nodes that transitively depend on the
// DAG's permanently failed node (including that node itself).
func failureFanOut(st *dag.DAGState) map[string]bool {
	out := map[string]bool{}
	if st.FailNode == "" {
		return out
	}
	adj := map[string][]string{}
	for i := range st.Spec.Nodes {
		n := &st.Spec.Nodes[i]
		for _, d := range n.Deps {
			adj[d] = append(adj[d], n.ID)
		}
	}
	queue := []string{st.FailNode}
	for len(queue) > 0 {
		cur := queue[0]
		queue = queue[1:]
		if out[cur] {
			continue
		}
		out[cur] = true
		queue = append(queue, adj[cur]...)
	}
	return out
}

func nodeSpec(st *dag.DAGState, id string) dag.NodeSpec {
	for i := range st.Spec.Nodes {
		if st.Spec.Nodes[i].ID == id {
			return st.Spec.Nodes[i]
		}
	}
	return dag.NodeSpec{}
}

func hasRecoverableWork(st *dag.DAGState) bool {
	doomed := failureFanOut(st)
	for id, ns := range st.Nodes {
		switch ns.Status {
		case dag.StatusRunning, dag.StatusPending:
			if st.Status == dag.DAGFailed && doomed[id] {
				continue
			}
			return true
		case dag.StatusFailed:
			if ns.Attempt < st.Spec.MaxAttemptsFor(nodeSpec(st, id), 1) {
				return true
			}
		}
	}
	return false
}

func newID() (string, error) {
	var b [8]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", err
	}
	return hex.EncodeToString(b[:]), nil
}

func mapGetErr(err error) error {
	if errors.Is(err, store.ErrNotFound) {
		return ErrNotFound
	}
	return err
}
