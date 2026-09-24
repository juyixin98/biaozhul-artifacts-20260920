package engine

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"sync"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"resumable-bt/internal/store"
	"resumable-bt/internal/stub"
	"resumable-bt/internal/tree"
)

// TickResult is the result of one tick attempt.
type TickResult struct {
	ExecutionID uuid.UUID `json:"execution_id"`
	Seq         int64     `json:"seq"`
	Status      string    `json:"status"` // running|success|failure|canceled
	Terminal    bool      `json:"terminal"`
	Interrupted bool      `json:"interrupted"`
	Note        string    `json:"note,omitempty"`
}

// NodeStateView is one node state in an execution snapshot.
type NodeStateView struct {
	NodeID      string          `json:"node_id"`
	Status      string          `json:"status"`
	Detail      json.RawMessage `json:"detail"`
	UpdatedTick int64           `json:"updated_tick"`
}

// InvocationView is one action invocation in an execution snapshot.
type InvocationView struct {
	NodeID      string          `json:"node_id"`
	StableKey   string          `json:"stable_key"`
	Action      string          `json:"action"`
	Idempotent  bool            `json:"idempotent"`
	Params      json.RawMessage `json:"params"`
	Status      string          `json:"status"`
	Attempt     int64           `json:"attempt"`
	Result      json.RawMessage `json:"result"`
	Error       string          `json:"error,omitempty"`
	Dispatches  int64           `json:"dispatches"`
	CreatedTick int64           `json:"created_tick"`
	UpdatedTick int64           `json:"updated_tick"`
}

// Snapshot is the full resumable state of one execution.
type Snapshot struct {
	Execution     store.ExecutionRow `json:"execution"`
	NodeStates    []NodeStateView    `json:"node_states"`
	Invocations   []InvocationView   `json:"invocations"`
	Ticks         []store.TickRow    `json:"ticks"`
	NextDueUnixMS int64              `json:"next_due_unix_ms,omitempty"`
}

// BeforeCommitHook is invoked inside the tick transaction just before commit.
// It is the deterministic seam for tick-interruption tests: blocking on
// blockCh suspends the tick with the row lock held until the hook closes
// blockCh or the tick context is canceled.
type BeforeCommitHook func(execID uuid.UUID, seq int64, blockCh <-chan struct{})

// Engine orchestrates ticks, runners and the auto-tick scheduler.
type Engine struct {
	st  *store.Store
	reg *stub.Registry

	runner *runner

	mu        sync.Mutex
	execLocks map[uuid.UUID]*sync.Mutex
	hook      BeforeCommitHook

	rootCtx    context.Context
	rootCancel context.CancelFunc

	tickCh  chan uuid.UUID
	workers sync.WaitGroup
	timers  map[uuid.UUID]*time.Timer
}

// New constructs the engine and starts background workers.
func New(st *store.Store, reg *stub.Registry) *Engine {
	ctx, cancel := context.WithCancel(context.Background())
	e := &Engine{
		st:         st,
		reg:        reg,
		execLocks:  map[uuid.UUID]*sync.Mutex{},
		rootCtx:    ctx,
		rootCancel: cancel,
		tickCh:     make(chan uuid.UUID, 256),
		timers:     map[uuid.UUID]*time.Timer{},
	}
	e.runner = newRunner(st, reg, e.enqueueTick)
	e.workers.Add(1)
	go e.tickLoop()
	return e
}

// SetBeforeCommitHook installs the test seam (nil to remove).
func (e *Engine) SetBeforeCommitHook(h BeforeCommitHook) {
	e.mu.Lock()
	e.hook = h
	e.mu.Unlock()
}

// Close stops the scheduler and cancels in-flight local workers. Database
// rows remain 'running' and are re-driven when a new engine starts.
func (e *Engine) Close() {
	e.rootCancel()
	e.runner.shutdown()
	e.workers.Wait()

	e.mu.Lock()
	for _, t := range e.timers {
		t.Stop()
	}
	e.timers = map[uuid.UUID]*time.Timer{}
	e.mu.Unlock()
}

// execLock returns the per-execution mutex serializing ticks in this process.
func (e *Engine) execLock(id uuid.UUID) *sync.Mutex {
	e.mu.Lock()
	defer e.mu.Unlock()
	m, ok := e.execLocks[id]
	if !ok {
		m = &sync.Mutex{}
		e.execLocks[id] = m
	}
	return m
}

// enqueueTick coalesces auto-tick requests: one pending signal per execution
// is enough; the loop always reads the freshest state.
func (e *Engine) enqueueTick(id uuid.UUID) {
	select {
	case e.tickCh <- id:
	case <-e.rootCtx.Done():
	}
}

func (e *Engine) tickLoop() {
	defer e.workers.Done()
	for {
		select {
		case <-e.rootCtx.Done():
			return
		case id := <-e.tickCh:
			// Drain coalesced duplicates.
			drained := true
			for drained {
				select {
				case <-e.tickCh:
				default:
					drained = false
				}
			}
			// rootCtx is about to be (or already is) canceled on shutdown;
			// never drive a tick with it, since a canceled context aborts the
			// transaction mid-statement. Use a fresh context instead; a tick
			// is a short, bounded operation.
			tickCtx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
			res, err := e.Tick(tickCtx, id, "auto")
			cancel()
			if err != nil {
				if errors.Is(err, store.ErrConflict) {
					continue
				}
				if errors.Is(err, store.ErrNotFound) {
					continue
				}
				log.Printf("auto tick %s: %v", id, err)
				// Back off briefly then re-arm so a transient DB error does
				// not wedge a still-running execution.
				select {
				case <-time.After(200 * time.Millisecond):
					e.enqueueTick(id)
				case <-e.rootCtx.Done():
					return
				}
				continue
			}
			_ = res
		}
	}
}

// armTimer schedules an auto-tick at t unless one is sooner already.
func (e *Engine) armTimer(id uuid.UUID, t time.Time) {
	d := time.Until(t)
	if d < 0 {
		d = 0
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	if old, ok := e.timers[id]; ok {
		old.Stop()
	}
	e.timers[id] = time.AfterFunc(d, func() { e.enqueueTick(id) })
}

func (e *Engine) dropTimer(id uuid.UUID) {
	e.mu.Lock()
	defer e.mu.Unlock()
	if t, ok := e.timers[id]; ok {
		t.Stop()
		delete(e.timers, id)
	}
}

// Publish parses/validates and publishes an immutable tree version.
func (e *Engine) Publish(ctx context.Context, name string, raw []byte) (store.VersionRow, bool, error) {
	def, err := tree.ParseDefinition(raw)
	if err != nil {
		return store.VersionRow{}, false, err
	}
	if err := def.Validate(e.reg.Known); err != nil {
		return store.VersionRow{}, false, err
	}
	hash, err := def.ContentHash()
	if err != nil {
		return store.VersionRow{}, false, err
	}
	return e.st.PublishTree(ctx, name, def, hash)
}

// StartExecution binds a new execution to a published version.
func (e *Engine) StartExecution(ctx context.Context, name string, version int64) (uuid.UUID, store.VersionRow, error) {
	if version == 0 {
		v, err := e.st.LatestVersion(ctx, name)
		if err != nil {
			return uuid.Nil, store.VersionRow{}, err
		}
		version = v
	}
	v, err := e.st.GetVersion(ctx, name, version)
	if err != nil {
		return uuid.Nil, store.VersionRow{}, err
	}
	id := uuid.New()
	row := store.ExecutionRow{
		ID:          id,
		TreeName:    name,
		TreeVersion: version,
		ContentHash: v.ContentHash,
		Status:      store.StatusRunning,
	}
	if err := e.st.CreateExecution(ctx, row); err != nil {
		return uuid.Nil, store.VersionRow{}, err
	}
	return id, v, nil
}

// Cancel cancels an execution and all its active local workers.
func (e *Engine) Cancel(ctx context.Context, id uuid.UUID) error {
	if err := e.st.MarkExecutionCanceled(ctx, id); err != nil {
		return err
	}
	e.dropTimer(id)
	e.runner.cancelAll(id)
	return nil
}

// walkPlan collects everything produced during one tree walk.
type walkPlan struct {
	def      *tree.Definition
	rootStat tree.Status
	saved    []store.NodeStateRow
	dispatch []store.InvocationRow // pending invocations to run after commit
	cancels  []string              // stable keys canceled within the tx
	nextDue  time.Time
	hasDue   bool
}

// Tick performs one serialized, numbered, transactional tick.
func (e *Engine) Tick(ctx context.Context, id uuid.UUID, note string) (TickResult, error) {
	mu := e.execLock(id)
	mu.Lock()
	defer mu.Unlock()

	var (
		seq  int64
		plan *walkPlan
		hook BeforeCommitHook
	)

	txErr := e.st.Tx(ctx, func(tx pgx.Tx) error {
		_, next, err := store.LockExecutionForTick(ctx, tx, id)
		if err != nil {
			return err
		}
		seq = next

		if err := store.InsertTick(ctx, tx, id, seq); err != nil {
			return err
		}

		exec, err := store.GetExecutionTx(ctx, tx, id)
		if err != nil {
			return err
		}
		ver, err := e.st.GetVersion(ctx, exec.TreeName, exec.TreeVersion)
		if err != nil {
			return err
		}
		def, err := tree.ParseDefinition(ver.Definition)
		if err != nil {
			return err
		}

		states, err := store.LoadNodeStates(ctx, tx, id)
		if err != nil {
			return err
		}

		wp := &walkPlan{def: def}
		rt := &runtime{
			tx:     tx,
			ctx:    ctx,
			execID: id,
			def:    def,
			seq:    seq,
			states: states,
			plan:   wp,
			now:    time.Now(),
		}
		rootStat, walkErr := rt.tickNode(def.Root)
		if walkErr != nil {
			return walkErr
		}
		wp.rootStat = rootStat

		// Persist all collected node states deterministically.
		sortSaved(wp.saved)
		for _, ns := range wp.saved {
			if err := store.UpsertNodeState(ctx, tx, id, seq, ns); err != nil {
				return err
			}
		}

		e.mu.Lock()
		hook = e.hook
		e.mu.Unlock()
		if hook != nil {
			block := make(chan struct{})
			hook(id, seq, block)
			select {
			case <-block:
			case <-ctx.Done():
				return ctx.Err()
			}
		}

		terminal := rootStat.IsTerminal()
		finalStatus := string(rootStat)
		if err := store.CommitTick(ctx, tx, id, seq, finalStatus, note, terminal); err != nil {
			return err
		}
		plan = wp
		return nil
	})

	if txErr != nil {
		if errors.Is(txErr, context.Canceled) || errors.Is(txErr, context.DeadlineExceeded) {
			// Tick interrupted before commit: roll back all changes, record the
			// interrupted attempt on a separate connection. The sequence is
			// NOT consumed (the tree resumes at the same seq next time).
			if seq > 0 {
				_ = e.st.InsertInterruptedTick(context.Background(), id, seq, note)
			}
			return TickResult{
				ExecutionID: id,
				Seq:         seq,
				Status:      "interrupted",
				Interrupted: true,
				Note:        "tick context canceled before commit; transaction rolled back",
			}, nil
		}
		return TickResult{ExecutionID: id, Seq: seq}, txErr
	}

	terminal := plan.rootStat.IsTerminal()
	res := TickResult{
		ExecutionID: id,
		Seq:         seq,
		Status:      string(plan.rootStat),
		Terminal:    terminal,
	}

	// Post-commit effects: cancel local workers of rows flipped to canceled,
	// and dispatch newly-pending invocations.
	if len(plan.cancels) > 0 {
		e.runner.cancelActive(id, plan.cancels)
	}
	for _, inv := range plan.dispatch {
		e.runner.dispatch(e.rootCtx, id, inv)
	}

	if terminal {
		e.dropTimer(id)
	} else if plan.hasDue {
		e.armTimer(id, plan.nextDue)
	} else {
		e.dropTimer(id)
	}
	return res, nil
}

// runtime is per-tick walk state.
type runtime struct {
	tx     pgx.Tx
	ctx    context.Context
	execID uuid.UUID
	def    *tree.Definition
	seq    int64
	states map[string]store.NodeStateRow
	plan   *walkPlan
	now    time.Time
}

func (rt *runtime) node(id string) nodeRuntime {
	nr := nodeRuntime{def: rt.def.Nodes[id]}
	if s, ok := rt.states[id]; ok {
		nr.status = tree.Status(s.Status)
		nr.detail = s.Detail
	}
	return nr
}

func (rt *runtime) save(id string, status tree.Status, detail any) {
	var raw []byte
	if detail != nil {
		raw = marshalDetail(detail)
	} else {
		raw = []byte("{}")
	}
	rt.plan.saved = append(rt.plan.saved, store.NodeStateRow{
		NodeID: id, Status: string(status), Detail: raw, UpdatedTick: rt.seq,
	})
}

func (rt *runtime) rememberDue(t time.Time) {
	if !rt.plan.hasDue || t.Before(rt.plan.nextDue) {
		rt.plan.nextDue = t
		rt.plan.hasDue = true
	}
}

// stableKey returns the deterministic, node-bound dedup key.
func stableKey(nodeID string) string {
	// Prefix and a short hash suffix make the key stable yet unambiguous; the
	// node id itself is unique inside an execution, so the hash is just a
	// normalization guard against exotic characters.
	sum := sha256.Sum256([]byte(nodeID))
	return "n:" + nodeID + ":" + hex.EncodeToString(sum[:4])
}

// tickNode dispatches on kind and returns the node's propagated status.
func (rt *runtime) tickNode(id string) (tree.Status, error) {
	n := rt.def.Nodes[id]
	switch n.Kind {
	case tree.KindAction:
		return rt.tickAction(id)
	case tree.KindSequence:
		return rt.tickSequence(id)
	case tree.KindFallback:
		return rt.tickFallback(id)
	case tree.KindParallel:
		return rt.tickParallel(id)
	case tree.KindTimeout:
		return rt.tickTimeout(id)
	default:
		return "", fmt.Errorf("unknown kind %q for node %q", n.Kind, id)
	}
}

// ---- Sequence -------------------------------------------------------------

func (rt *runtime) tickSequence(id string) (tree.Status, error) {
	n := rt.def.Nodes[id]
	nr := rt.node(id)
	var d compositeDetail
	nr.detailFor(&d)
	if nr.status == tree.Success || nr.status == tree.Failure {
		// Composite result already persisted: children are terminal as well.
		return nr.status, nil
	}
	if d.Active > len(n.Children) {
		d.Active = 0
	}
	for i := d.Active; i < len(n.Children); i++ {
		s, err := rt.tickNode(n.Children[i])
		if err != nil {
			return "", err
		}
		switch s {
		case tree.Failure:
			rt.save(id, tree.Failure, nil)
			return tree.Failure, nil
		case tree.Running:
			rt.save(id, tree.Running, compositeDetail{Active: i})
			return tree.Running, nil
		}
		// success: advance to next child within the same tick.
	}
	rt.save(id, tree.Success, nil)
	return tree.Success, nil
}

// ---- Fallback -------------------------------------------------------------

func (rt *runtime) tickFallback(id string) (tree.Status, error) {
	n := rt.def.Nodes[id]
	nr := rt.node(id)
	var d compositeDetail
	nr.detailFor(&d)
	if nr.status == tree.Success || nr.status == tree.Failure {
		return nr.status, nil
	}
	if d.Active > len(n.Children) {
		d.Active = 0
	}
	for i := d.Active; i < len(n.Children); i++ {
		s, err := rt.tickNode(n.Children[i])
		if err != nil {
			return "", err
		}
		switch s {
		case tree.Success:
			rt.save(id, tree.Success, nil)
			return tree.Success, nil
		case tree.Running:
			rt.save(id, tree.Running, compositeDetail{Active: i})
			return tree.Running, nil
		}
		// failure: try the next branch within the same tick.
	}
	rt.save(id, tree.Failure, nil)
	return tree.Failure, nil
}

// ---- Parallel -------------------------------------------------------------

func (rt *runtime) tickParallel(id string) (tree.Status, error) {
	n := rt.def.Nodes[id]
	nr := rt.node(id)
	if nr.status == tree.Success || nr.status == tree.Failure {
		return nr.status, nil
	}

	results := make([]tree.Status, len(n.Children))
	for i, cid := range n.Children {
		cs := rt.node(cid).status
		if cs == tree.Success || cs == tree.Failure {
			results[i] = cs
			continue
		}
		s, err := rt.tickNode(cid)
		if err != nil {
			return "", err
		}
		results[i] = s
	}

	succ, fail := 0, 0
	activeChildren := []string{}
	for i, s := range results {
		switch s {
		case tree.Success:
			succ++
		case tree.Failure:
			fail++
		default:
			activeChildren = append(activeChildren, n.Children[i])
		}
	}

	if succ >= n.SuccessThreshold {
		if err := rt.cancelDescendants(id, activeChildren); err != nil {
			return "", err
		}
		rt.save(id, tree.Success, nil)
		return tree.Success, nil
	}
	if fail >= n.FailureThreshold {
		if err := rt.cancelDescendants(id, activeChildren); err != nil {
			return "", err
		}
		rt.save(id, tree.Failure, nil)
		return tree.Failure, nil
	}
	rt.save(id, tree.Running, parallelDetail{Successes: succ, Failures: fail})
	return tree.Running, nil
}

// ---- Timeout --------------------------------------------------------------

func (rt *runtime) tickTimeout(id string) (tree.Status, error) {
	n := rt.def.Nodes[id]
	nr := rt.node(id)
	if nr.status == tree.Success || nr.status == tree.Failure {
		return nr.status, nil
	}
	var d timeoutDetail
	nr.detailFor(&d)
	if d.DeadlineUnixMS == 0 {
		d.DeadlineUnixMS = rt.now.Add(time.Duration(n.TimeoutMS) * time.Millisecond).UnixMilli()
	}
	deadline := time.UnixMilli(d.DeadlineUnixMS)
	rt.rememberDue(deadline)

	if !rt.now.Before(deadline) {
		// Timeout wins: cancel everything still running below.
		if err := rt.cancelDescendants(id, n.Children); err != nil {
			return "", err
		}
		rt.save(id, tree.Failure, nil)
		return tree.Failure, nil
	}

	s, err := rt.tickNode(n.Children[0])
	if err != nil {
		return "", err
	}
	if s.IsTerminal() {
		rt.save(id, s, nil)
		return s, nil
	}
	rt.save(id, tree.Running, d)
	return tree.Running, nil
}

// cancelDescendants marks every still-running action invocation under the
// given child subtrees canceled (SQL, inside the tick tx), force-marks their
// node states failure for snapshot coherence, and records the stable keys
// whose in-process workers must be interrupted after commit.
func (rt *runtime) cancelDescendants(parentID string, childRoots []string) error {
	keys := map[string]bool{}
	nodeIDs := map[string]bool{}
	var walk func(string)
	walk = func(id string) {
		nodeIDs[id] = true
		n := rt.def.Nodes[id]
		if n.Kind == tree.KindAction {
			keys[stableKey(id)] = true
		}
		for _, c := range n.Children {
			walk(c)
		}
	}
	for _, c := range childRoots {
		walk(c)
	}

	keyList := make([]string, 0, len(keys))
	for k := range keys {
		keyList = append(keyList, k)
	}
	canceled, err := store.CancelInvocationsByStableKeys(rt.ctx, rt.tx, rt.execID, keyList)
	if err != nil {
		return fmt.Errorf("cancel invocations under %s: %w", parentID, err)
	}
	rt.plan.cancels = append(rt.plan.cancels, canceled...)

	// Force-fail non-terminal node states of the canceled subtrees so a
	// snapshot never shows nodes still running under a terminal parent.
	for nid := range nodeIDs {
		if s, ok := rt.states[nid]; ok && s.Status == string(tree.Running) {
			rt.save(nid, tree.Failure, map[string]any{"reason": "canceled_by_parent"})
		}
	}
	return nil
}

// ---- Action ---------------------------------------------------------------

func (rt *runtime) tickAction(id string) (tree.Status, error) {
	n := rt.def.Nodes[id]
	nr := rt.node(id)
	key := stableKey(id)

	if nr.status == tree.Success {
		rt.save(id, tree.Success, nr.detailOrEmpty())
		return tree.Success, nil
	}
	if nr.status == tree.Failure {
		rt.save(id, tree.Failure, nr.detailOrEmpty())
		return tree.Failure, nil
	}

	existing, err := store.LoadInvocation(rt.ctx, rt.tx, rt.execID, key)
	if err != nil && !errors.Is(err, store.ErrNotFound) {
		return "", fmt.Errorf("load invocation %s: %w", id, err)
	}

	if errors.Is(err, store.ErrNotFound) {
		params, _ := json.Marshal(n.Params)
		ins := store.InvocationInsert{
			NodeID:      id,
			StableKey:   key,
			Action:      n.Action,
			Idempotent:  n.Idempotent,
			Params:      params,
			CreatedTick: rt.seq,
		}
		if err := store.InsertInvocation(rt.ctx, rt.tx, rt.execID, ins); err != nil {
			return "", fmt.Errorf("insert invocation %s: %w", id, err)
		}
		rt.save(id, tree.Running, map[string]any{"stable_key": key})
		// Collected for post-commit dispatch; the row is pending in the tx.
		rt.plan.dispatch = append(rt.plan.dispatch, store.InvocationRow{
			NodeID:     id,
			StableKey:  key,
			Action:     n.Action,
			Idempotent: n.Idempotent,
			Params:     params,
			Status:     "pending",
		})
		return tree.Running, nil
	}

	switch existing.Status {
	case "success":
		// A succeeded action is never re-executed — the central non-idempotent
		// guarantee; resumed straight from its persisted result.
		rt.save(id, tree.Success, map[string]any{"stable_key": key})
		return tree.Success, nil
	case "failure":
		rt.save(id, tree.Failure, map[string]any{"stable_key": key, "error": existing.Err})
		return tree.Failure, nil
	case "canceled":
		// The parent canceled this invocation; it stays failed and can never
		// come back, even if a late worker report arrives.
		rt.save(id, tree.Failure, map[string]any{"stable_key": key, "reason": "canceled"})
		return tree.Failure, nil
	default:
		// pending (dispatch post-commit was lost to a crash mid-tick) or
		// running (worker died with the process): re-drive after commit.
		rt.save(id, tree.Running, map[string]any{"stable_key": key})
		rt.plan.dispatch = append(rt.plan.dispatch, existing)
		return tree.Running, nil
	}
}

func sortSaved(rows []store.NodeStateRow) {
	for i := 1; i < len(rows); i++ {
		for j := i; j > 0 && rows[j-1].NodeID > rows[j].NodeID; j-- {
			rows[j-1], rows[j] = rows[j], rows[j-1]
		}
	}
}
