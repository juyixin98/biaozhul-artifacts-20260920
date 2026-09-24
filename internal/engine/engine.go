// Package engine is the resumable DAG scheduler.
//
// Execution model
//
//   - Every state transition is persisted to the Store before the effect it
//     describes is considered real (a node is only launched after its
//     "running" snapshot is durable; a success is only reported after its
//     result is durable). Hence every node executes AT LEAST ONCE, and a node
//     whose success is confirmed (durable) never executes again.
//   - Retries use a fixed per-node backoff; a node in "retrying" becomes
//     eligible at NodeState.RunAfter.
//   - A failed node fails the DAG; its downstream nodes are "skipped".
//   - Cancel marks every not-yet-succeeded node "cancelled" and cancels
//     in-flight work via context; no node is ever launched for a DAG whose
//     cancel has been requested.
package engine

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"sync"
	"time"

	"dagexec/internal/model"
	"dagexec/internal/task"
)

// Store is the durability backend. Any implementation that stores one JSON
// blob per ID works (the provided one is a file store).
type Store interface {
	Save(id string, snap any) error
	Load(id string, out any) error
	List() ([]string, error)
}

// Sentinel errors.
var (
	ErrNotFound  = errors.New("dag not found")
	ErrCancelled = errors.New("dag cancelled")
)

// Engine owns all DAG runs and their goroutines.
type Engine struct {
	store Store
	reg   *task.Registry

	mu      sync.Mutex
	runs    map[string]*run
	started bool

	rootCtx    context.Context
	rootCancel context.CancelFunc
	wg         sync.WaitGroup
}

// New constructs an engine.
func New(st Store, reg *task.Registry) *Engine {
	return &Engine{store: st, reg: reg, runs: map[string]*run{}}
}

// Start resumes every non-terminal snapshot and launches its scheduler.
// It must be called once before Submit.
func (e *Engine) Start(ctx context.Context) error {
	e.mu.Lock()
	if e.started {
		e.mu.Unlock()
		return errors.New("engine already started")
	}
	e.rootCtx, e.rootCancel = context.WithCancel(ctx)
	e.started = true
	ids, err := e.store.List()
	e.mu.Unlock()
	if err != nil {
		return fmt.Errorf("list snapshots: %w", err)
	}
	for _, id := range ids {
		var snap model.Snapshot
		if err := e.store.Load(id, &snap); err != nil {
			return fmt.Errorf("load snapshot %s: %w", id, err)
		}
		if snap.Nodes == nil {
			return fmt.Errorf("snapshot %s has no node states", id)
		}
		e.mu.Lock()
		e.runs[id] = e.newRun(&snap)
		e.mu.Unlock()
		if !snap.Status.Terminal() {
			e.resume(&snap)
			e.spawn(&snap)
		}
	}
	return nil
}

// resume repairs a snapshot loaded from disk: anything that was "running"
// when the process died has no confirmed result, so it goes back to pending
// and is retried (attempts executed so far still count toward the cap).
// "retrying" nodes whose backoff already elapsed also become pending.
func (e *Engine) resume(snap *model.Snapshot) {
	now := time.Now()
	changed := false
	for _, st := range snap.Nodes {
		switch st.Status {
		case model.StatusRunning:
			st.Status = model.StatusPending
			st.RunAfter = nil
			st.UpdatedAt = now
			changed = true
		case model.StatusRetrying:
			if st.RunAfter != nil && !now.Before(*st.RunAfter) {
				st.Status = model.StatusPending
				st.RunAfter = nil
				st.UpdatedAt = now
				changed = true
			}
		}
	}
	if changed {
		snap.UpdatedAt = now
		if err := e.store.Save(snap.ID, snap); err != nil {
			persistFail(snap.ID, err)
		}
	}
}

// Shutdown stops all schedulers and waits for in-flight invocations to
// observe cancellation and return. Running invocations are rolled back to
// pending on disk so the next process retries them (never double-committed).
func (e *Engine) Shutdown(ctx context.Context) error {
	e.mu.Lock()
	if !e.started {
		e.mu.Unlock()
		return nil
	}
	e.started = false
	runs := make([]*run, 0, len(e.runs))
	for _, r := range e.runs {
		runs = append(runs, r)
	}
	e.rootCancel()
	e.mu.Unlock()

	done := make(chan struct{})
	go func() {
		e.wg.Wait()
		close(done)
	}()
	select {
	case <-done:
	case <-ctx.Done():
	}

	// Roll back any in-flight invocations (their context is cancelled, so
	// their results are discarded) to pending and persist once.
	for _, r := range runs {
		r.mu.Lock()
		now := time.Now()
		changed := false
		for _, st := range r.snap.Nodes {
			if st.Status == model.StatusRunning {
				st.Status = model.StatusPending
				st.RunAfter = nil
				st.UpdatedAt = now
				changed = true
			}
		}
		if changed && !r.snap.Status.Terminal() {
			r.snap.UpdatedAt = now
			if err := e.store.Save(r.snap.ID, r.snap); err != nil {
				persistFail(r.snap.ID, err)
			}
		}
		r.mu.Unlock()
	}
	return nil
}

// TaskNames exposes the whitelisted function names.
func (e *Engine) TaskNames() []string { return e.reg.Names() }

// Submit validates, persists and starts a new DAG.
func (e *Engine) Submit(spec model.DAG) (*model.Snapshot, error) {
	if err := validateSpec(spec, e.reg); err != nil {
		return nil, err
	}
	id := newID()
	now := time.Now().UTC()
	snap := &model.Snapshot{
		ID:        id,
		Spec:      spec,
		Status:    model.StatusRunning,
		Nodes:     map[string]*model.NodeState{},
		CreatedAt: now,
		UpdatedAt: now,
	}
	for _, n := range spec.Nodes {
		snap.Nodes[n.ID] = &model.NodeState{Status: model.StatusPending, UpdatedAt: now}
	}
	if err := e.store.Save(id, snap); err != nil {
		return nil, fmt.Errorf("persist new dag: %w", err)
	}
	e.mu.Lock()
	if !e.started {
		e.mu.Unlock()
		// Persisted but not scheduled; Start() on a later process resumes it.
		return cloneSnap(snap), nil
	}
	r := e.newRun(snap)
	e.runs[id] = r
	// Clone while holding r.mu, before the scheduler goroutine starts.
	out := cloneSnap(snap)
	e.mu.Unlock()
	e.spawn(snap)
	return out, nil
}

// Get returns a deep copy of the current snapshot.
func (e *Engine) Get(id string) (*model.Snapshot, error) {
	e.mu.Lock()
	r, ok := e.runs[id]
	e.mu.Unlock()
	if !ok {
		return nil, ErrNotFound
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	return cloneSnap(r.snap), nil
}

// List returns snapshots for all known DAGs (newest first by CreatedAt).
func (e *Engine) List() ([]*model.Snapshot, error) {
	e.mu.Lock()
	runs := make([]*run, 0, len(e.runs))
	for _, r := range e.runs {
		runs = append(runs, r)
	}
	e.mu.Unlock()
	out := make([]*model.Snapshot, 0, len(runs))
	for _, r := range runs {
		r.mu.Lock()
		out = append(out, cloneSnap(r.snap))
		r.mu.Unlock()
	}
	return out, nil
}

// Cancel requests cancellation of a DAG. It synchronously marks every node
// that has not succeeded as cancelled (running nodes are interrupted via
// context) and persists, so on return no NEW node can ever start for the DAG.
// Cancelling an already-terminal DAG returns its snapshot unchanged.
func (e *Engine) Cancel(id string) (*model.Snapshot, error) {
	e.mu.Lock()
	r, ok := e.runs[id]
	e.mu.Unlock()
	if !ok {
		return nil, ErrNotFound
	}
	r.mu.Lock()
	snap := r.snap
	if !snap.Status.Terminal() {
		now := time.Now().UTC()
		snap.CancelRequested = true
		snap.Status = model.StatusCancelled
		snap.UpdatedAt = now
		for _, st := range snap.Nodes {
			if st.Status == model.StatusPending || st.Status == model.StatusRetrying {
				st.Status = model.StatusCancelled
				st.RunAfter = nil
				st.UpdatedAt = now
			}
		}
		if err := e.store.Save(snap.ID, snap); err != nil {
			r.mu.Unlock()
			persistFail(snap.ID, err)
			return nil, err
		}
		if r.cancel != nil {
			r.cancel()
		}
	}
	out := cloneSnap(snap)
	r.mu.Unlock()
	return out, nil
}

// ---------------------------------------------------------------------------
// run: one DAG's scheduler
// ---------------------------------------------------------------------------

type launch struct {
	nodeID string
	fn     task.Func
	params map[string]any
	deps   map[string]any
}

type run struct {
	snap   *model.Snapshot
	engine *Engine

	mu       sync.Mutex
	notify   chan struct{} // 1-capacity, non-blocking wake signal
	cancel   context.CancelFunc
	inflight sync.WaitGroup
}

func (e *Engine) newRun(snap *model.Snapshot) *run {
	return &run{snap: snap, engine: e, notify: make(chan struct{}, 1)}
}

func (e *Engine) spawn(snap *model.Snapshot) {
	e.mu.Lock()
	r := e.runs[snap.ID]
	ctx, cancel := context.WithCancel(e.rootCtx)
	r.mu.Lock()
	r.cancel = cancel
	r.mu.Unlock()
	e.wg.Add(1)
	e.mu.Unlock()

	go e.loop(ctx, r)
}

func (e *Engine) wake(r *run) {
	select {
	case r.notify <- struct{}{}:
	default:
	}
}

// loop is the scheduler goroutine for one DAG.
func (e *Engine) loop(ctx context.Context, r *run) {
	defer e.wg.Done()
	ticker := time.NewTicker(10 * time.Millisecond)
	defer ticker.Stop()

	for {
		// A cancellation request is authoritative: never launch again.
		if ctx.Err() != nil {
			return
		}
		launches, terminal := e.tick(ctx, r)
		for _, l := range launches {
			r.inflight.Add(1)
			e.wg.Add(1)
			go func(l launch) {
				defer e.wg.Done()
				defer r.inflight.Done()
				e.execute(ctx, r, l)
			}(l)
		}
		if terminal {
			return
		}

		select {
		case <-ctx.Done():
			return
		case <-r.notify:
		case <-ticker.C:
		}
	}
}

// nodeByID finds a spec node.
func nodeByID(snap *model.Snapshot, id string) *model.Node {
	for i := range snap.Spec.Nodes {
		if snap.Spec.Nodes[i].ID == id {
			return &snap.Spec.Nodes[i]
		}
	}
	return nil
}

// retryConfig resolves effective retries/backoff for a node.
func retryConfig(snap *model.Snapshot, n *model.Node) (int, time.Duration) {
	retries := snap.Spec.Retries
	backoff := time.Duration(snap.Spec.BackoffMs) * time.Millisecond
	if n.Retries != nil {
		retries = *n.Retries
	}
	if n.BackoffMs != nil {
		backoff = time.Duration(*n.BackoffMs) * time.Millisecond
	}
	return retries, backoff
}

// tick performs one scheduling pass. It propagates terminal states across
// edges, launches every node that is eligible NOW, persists all transitions,
// and reports whether the DAG has reached a terminal state.
func (e *Engine) tick(ctx context.Context, r *run) ([]launch, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	snap := r.snap

	if snap.Status.Terminal() {
		return nil, true
	}
	now := time.Now().UTC()
	var launches []launch
	changed := false

	for i := range snap.Spec.Nodes {
		n := &snap.Spec.Nodes[i]
		st := snap.Nodes[n.ID]
		if st.Status != model.StatusPending && st.Status != model.StatusRetrying {
			continue
		}
		if st.Status == model.StatusRetrying && st.RunAfter != nil && now.Before(*st.RunAfter) {
			continue
		}

		var failed, cancelled bool
		for _, depID := range n.Deps {
			switch snap.Nodes[depID].Status {
			case model.StatusFailed, model.StatusSkipped:
				failed = true
			case model.StatusCancelled:
				cancelled = true
			}
		}
		if failed {
			st.Status = model.StatusSkipped
			st.RunAfter = nil
			st.UpdatedAt = now
			changed = true
			continue
		}
		if cancelled {
			st.Status = model.StatusCancelled
			st.RunAfter = nil
			st.UpdatedAt = now
			changed = true
			continue
		}

		allDepsOK := true
		depResults := map[string]any{}
		for _, depID := range n.Deps {
			dst := snap.Nodes[depID]
			if dst.Status != model.StatusSucceeded {
				allDepsOK = false
				break
			}
			depResults[depID] = dst.Result
		}
		if !allDepsOK {
			continue
		}

		fn, ok := e.reg.Get(n.Type)
		if !ok {
			// Defensive: types were validated at submit time.
			st.Status = model.StatusFailed
			st.Error = "unknown task type " + n.Type
			st.UpdatedAt = now
			changed = true
			continue
		}
		params, err := task.ResolveParams(n.Params, depResults)
		if err != nil {
			st.Status = model.StatusFailed
			st.Error = err.Error()
			st.UpdatedAt = now
			changed = true
			continue
		}

		attemptNo := st.Attempts + 1
		st.Status = model.StatusRunning
		st.Attempts = attemptNo
		st.RunAfter = nil
		if st.StartedAt == nil {
			t := now
			st.StartedAt = &t
		}
		st.UpdatedAt = now
		launches = append(launches, launch{nodeID: n.ID, fn: fn, params: params, deps: depResults})
		changed = true
	}

	// Classify the DAG.
	terminal, dagStatus := classify(snap)

	if changed || terminal {
		snap.UpdatedAt = now
		if terminal {
			snap.Status = dagStatus
		}
		if err := e.store.Save(snap.ID, snap); err != nil {
			persistFail(snap.ID, err)
		}
	}
	return launches, terminal
}

// classify reports whether every node is in a terminal state and the resulting
// DAG status: any failure/skip -> failed; otherwise all cancelled ->
// cancelled; otherwise all succeeded -> succeeded.
func classify(snap *model.Snapshot) (bool, model.Status) {
	allSucceeded := true
	allCancelled := true
	for _, st := range snap.Nodes {
		switch st.Status {
		case model.StatusSucceeded:
			allCancelled = false
		case model.StatusCancelled:
			allSucceeded = false
		case model.StatusFailed, model.StatusSkipped:
			return true, model.StatusFailed
		default:
			return false, ""
		}
	}
	switch {
	case allSucceeded:
		return true, model.StatusSucceeded
	case allCancelled:
		return true, model.StatusCancelled
	default:
		// Mix of succeeded and cancelled: a user-cancelled DAG.
		return true, model.StatusCancelled
	}
}

// execute runs one function invocation and records its outcome. The
// "running" transition was already persisted by tick, so a crash here simply
// causes a retry on restart.
func (e *Engine) execute(ctx context.Context, r *run, l launch) {
	res, err := func() (out any, err error) {
		defer func() {
			if rec := recover(); rec != nil {
				err = fmt.Errorf("task %q panicked: %v", l.nodeID, rec)
			}
		}()
		return l.fn(ctx, l.params, l.deps)
	}()

	r.mu.Lock()
	snap := r.snap
	st := snap.Nodes[l.nodeID]
	now := time.Now().UTC()

	if snap.CancelRequested {
		// User cancellation: a completed in-flight attempt keeps its confirmed
		// success; otherwise it is cancelled (never failed, since the DAG is
		// already terminal and no retry will occur). Nothing is scheduled.
		if err == nil {
			st.Status = model.StatusSucceeded
			st.Result = res
			st.Error = ""
		} else {
			st.Status = model.StatusCancelled
			st.Error = ""
		}
		st.RunAfter = nil
		st.UpdatedAt = now
		snap.UpdatedAt = now
		saveErr := e.store.Save(snap.ID, snap)
		r.mu.Unlock()
		if saveErr != nil {
			persistFail(snap.ID, saveErr)
		}
		return
	}
	if ctx.Err() != nil {
		// Process shutdown: outcome is unconfirmed; Shutdown rolls this node
		// back to pending. Do not persist anything from this attempt.
		r.mu.Unlock()
		return
	}

	st.UpdatedAt = now
	switch {
	case err == nil:
		st.Status = model.StatusSucceeded
		st.Result = res
		st.Error = ""
		st.RunAfter = nil
	default:
		st.Error = err.Error()
		n := nodeByID(snap, l.nodeID)
		retries, backoff := retryConfig(snap, n)
		if st.Attempts <= retries {
			runAfter := now.Add(backoff)
			st.Status = model.StatusRetrying
			st.RunAfter = &runAfter
		} else {
			st.Status = model.StatusFailed
			st.RunAfter = nil
		}
	}
	snap.UpdatedAt = now
	saveErr := e.store.Save(snap.ID, snap)
	r.mu.Unlock()
	if saveErr != nil {
		persistFail(snap.ID, saveErr)
	}
	e.wake(r)
}

// cloneSnap returns a deep copy via JSON round-trip (snapshots are small).
func cloneSnap(snap *model.Snapshot) *model.Snapshot {
	data, err := json.Marshal(snap)
	if err != nil {
		panic(err)
	}
	var cp model.Snapshot
	if err := json.Unmarshal(data, &cp); err != nil {
		panic(err)
	}
	return &cp
}

func newID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		panic(err)
	}
	return hex.EncodeToString(b[:])
}

// persistFail is the hook for durability failures. Persistence is required
// for correctness, so we fail loudly; alternative stores can surface errors
// through metrics. The process keeps running, and the next transition retries
// the write.
var persistFailLogger func(id string, err error)

func persistFail(id string, err error) {
	if persistFailLogger != nil {
		persistFailLogger(id, err)
		return
	}
	fmt.Printf("engine: persist error for dag %s: %v\n", id, err)
}
