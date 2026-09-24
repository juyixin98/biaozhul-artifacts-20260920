package engine

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"

	"bt/internal/model"
	"bt/internal/store"
	"bt/internal/stub"
)

// ReapOrphans must run once at process startup before serving traffic. It
// marks every action call left non-terminal by a previous process as
// interrupted (so the next tick re-runs it — a running action has produced no
// durable success) and closes any ticks left open. Terminal executions are
// untouched.
func (e *Engine) ReapOrphans(ctx context.Context) error {
	_, err := e.st.DB().Exec(ctx, `
		UPDATE action_calls
		   SET status = 'interrupted', updated_seq = updated_seq
		 WHERE status = 'running'
		   AND execution_id IN (SELECT id FROM executions WHERE status = 'running')`)
	if err != nil {
		return fmt.Errorf("reap orphan calls: %w", err)
	}
	_, err = e.st.DB().Exec(ctx, `
		UPDATE ticks
		   SET status = 'interrupted', ended_at = now()
		 WHERE status IS DISTINCT FROM 'completed' AND ended_at IS NULL`)
	if err != nil {
		return fmt.Errorf("reap open ticks: %w", err)
	}
	return nil
}

// Abort permanently aborts an execution and cancels every still-running
// action attempt in this process. Late results afterwards are fenced.
func (e *Engine) Abort(ctx context.Context, execID string) (*SnapshotDTO, error) {
	exec, _, err := e.st.ExecutionTree(ctx, execID)
	if err != nil {
		return nil, err
	}
	if exec.Status != "running" {
		return nil, store.ErrAlreadyTerminal
	}

	pool := e.st.DB()

	// Cancel the execution runtime BEFORE taking the advisory lock: an
	// in-flight tick may be parked inside a synchronous stub; canceling its
	// runtime context makes it interrupt, release the lock and finish, after
	// which this abort can proceed.
	e.execMu.Lock()
	rt := e.execCtx[execID]
	e.execMu.Unlock()
	if rt != nil {
		rt.cancel()
	}

	lockConn, err := pool.Acquire(ctx)
	if err != nil {
		return nil, err
	}
	defer lockConn.Release()
	if _, err := lockConn.Exec(ctx,
		`SELECT pg_advisory_lock($1)`, advisoryKey(execID)); err != nil {
		return nil, err
	}
	defer func() {
		_, _ = lockConn.Exec(context.Background(), `SELECT pg_advisory_unlock($1)`, advisoryKey(execID))
	}()

	tx, err := pool.Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx)

	var locked string
	err = tx.QueryRow(ctx, `SELECT status FROM executions WHERE id = $1 FOR UPDATE`, execID).Scan(&locked)
	if err != nil {
		return nil, err
	}
	if locked != "running" {
		return nil, store.ErrAlreadyTerminal
	}
	if _, err := tx.Exec(ctx,
		`UPDATE executions SET status='aborted', updated_at=now() WHERE id=$1`, execID); err != nil {
		return nil, err
	}
	if _, err := tx.Exec(ctx,
		`UPDATE node_states SET status='canceled' WHERE execution_id=$1 AND status='running'`, execID); err != nil {
		return nil, err
	}
	if _, err := tx.Exec(ctx,
		`UPDATE action_calls SET status='canceled' WHERE execution_id=$1 AND status IN ('running','interrupted')`, execID); err != nil {
		return nil, err
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, err
	}

	return e.Snapshot(ctx, execID)
}

// Tick performs one tick for the execution.
//
// reqCtx is the request context: if it is canceled mid-tick (client gone /
// short deadline) the tick is recorded as interrupted: its tree-state
// transaction rolls back, while committed action sagas remain. Every tick
// consumes one durable sequence value, including interrupted ones.
func (e *Engine) Tick(reqCtx context.Context, execID string) (*TickResult, error) {
	exec, treeRow, err := e.st.ExecutionTree(reqCtx, execID)
	if err != nil {
		return nil, err
	}
	if exec.Status != "running" {
		return nil, store.ErrAlreadyTerminal
	}

	pool := e.st.DB()
	// A dedicated connection holds the execution-scoped advisory lock for
	// the whole tick, serializing ticks across HTTP handlers and processes.
	lockConn, err := pool.Acquire(reqCtx)
	if err != nil {
		return nil, err
	}
	defer lockConn.Release()

	if _, err := lockConn.Exec(reqCtx,
		`SELECT pg_advisory_lock($1)`, advisoryKey(execID)); err != nil {
		return nil, err
	}
	locked := true
	unlock := func() {
		if locked {
			_, _ = lockConn.Exec(context.Background(), `SELECT pg_advisory_unlock($1)`, advisoryKey(execID))
			locked = false
		}
	}
	defer unlock()

	// Re-check execution status under the lock (could have been aborted).
	var status string
	if err := lockConn.QueryRow(reqCtx,
		`SELECT status FROM executions WHERE id=$1`, execID).Scan(&status); err != nil {
		return nil, err
	}
	if status != "running" {
		return nil, store.ErrAlreadyTerminal
	}

	rt := e.runtimeFor(execID)
	tickID := randomID("tick")

	// Allocate the durable sequence and open the tick row immediately, so an
	// interrupted or crashed tick still consumes a durable number.
	var seq int64
	if err := lockConn.QueryRow(reqCtx,
		`INSERT INTO ticks(id, execution_id, seq, status, started_at)
		 VALUES ($1,$2,
		         COALESCE((SELECT MAX(seq)+1 FROM ticks WHERE execution_id=$2),1),
		         'interrupted', now())
		 RETURNING seq`, tickID, execID).Scan(&seq); err != nil {
		return nil, err
	}

	states, calls, err := e.st.LoadSnapshot(reqCtx, execID)
	if err != nil {
		return nil, err
	}

	// tickCtx is canceled by request cancellation (tick interruption) OR by
	// execution abort (rt.ctx). Synchronous stub attempts derive from it, so
	// either event unblocks a parked synchronous tick promptly.
	tickCtx, cancel := context.WithCancel(reqCtx)
	stopLink := context.AfterFunc(rt.ctx, cancel)
	defer stopLink()
	defer cancel()
	tc := &tickContext{
		engine:    e,
		execID:    execID,
		tree:      treeRow.Tree,
		tickID:    tickID,
		seq:       seq,
		ctx:       tickCtx,
		rt:        rt,
		states:    states,
		calls:     calls,
		launched:  map[string]int{},
		deadlines: map[string]pgtypeTimestamptz{},
		canceled:  map[string]bool{},
	}

	// Evaluate. Action sagas commit independently inside evalAction; node
	// state is held in memory until finalize.
	rootStatus := model.StatusRunning
	evalErr := tc.check()
	if evalErr == nil {
		rootStatus = evalNode(tc, treeRow.Tree.Root)
		evalErr = tc.check()
	}

	finalTreeStatus := "running"
	interrupted := false
	switch {
	case errors.Is(evalErr, errInterrupted), errors.Is(evalErr, errAborted):
		interrupted = true
	case evalErr != nil:
		return nil, evalErr
	case rootStatus == model.StatusSuccess:
		finalTreeStatus = "success"
	case rootStatus == model.StatusFailure:
		finalTreeStatus = "failure"
	case rootStatus == model.StatusCanceled:
		// Whole subtree canceled without a decided outcome only happens on
		// abort; treat as failure otherwise.
		finalTreeStatus = "failure"
	}

	// Finalize tree state transactionally.
	if interrupted {
		// Ensure node-side cancellation bookkeeping for async attempts that
		// were started and got canceled by request cancellation is saga
		// recorded as interrupted (launch itself already committed rows).
		e.markTickInterruptedAttempts(tc)
		// reqCtx is already canceled by definition; finalize using a fresh
		// context (still holding the advisory lock on lockConn).
		finalizeCtx, finalizeCancel := context.WithTimeout(context.Background(), 10*time.Second)
		if _, err := lockConn.Exec(finalizeCtx,
			`UPDATE ticks SET status='interrupted', tree_status=NULL, ended_at=now()
			 WHERE id=$1`, tickID); err != nil {
			finalizeCancel()
			return nil, err
		}
		finalizeCancel()
		unlock()
		return &TickResult{
			TickID:     tickID,
			Seq:        seq,
			Status:     "interrupted",
			TreeStatus: "running",
			Nodes:      tc.nodeMapSnapshot(),
			Calls:      tc.callMapSnapshot(),
		}, nil
	}

	if err := tc.commit(finalTreeStatus); err != nil {
		return nil, err
	}
	unlock()

	return &TickResult{
		TickID:     tickID,
		Seq:        seq,
		Status:     "completed",
		TreeStatus: finalTreeStatus,
		Nodes:      tc.nodeMapSnapshot(),
		Calls:      tc.callMapSnapshot(),
	}, nil
}

// commit writes all node-state changes and terminal execution status in one
// transaction (the advisory lock from the tick connection still serializes
// against concurrent ticks because the lock is session/connection scoped and
// pool transactions on other connections block acquiring it).
func (tc *tickContext) commit(treeStatus string) error {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	pool := tc.engine.st.DB()
	tx, err := pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)

	// Upsert every touched node state. Deadlines for timeout nodes are
	// preserved (and newly initialized here).
	for _, ps := range dedupePending(tc.pendingSets) {
		r := tc.states[ps.nodeID]
		var dlArg any
		if r != nil && r.DeadlineAt != nil {
			dlArg = *r.DeadlineAt
		}
		if _, err := tx.Exec(ctx, `
			INSERT INTO node_states(execution_id, node_id, status, updated_seq, deadline_at)
			VALUES ($1,$2,$3,$4,$5)
			ON CONFLICT (execution_id, node_id)
			DO UPDATE SET status=EXCLUDED.status, updated_seq=EXCLUDED.updated_seq,
			              deadline_at=COALESCE(node_states.deadline_at, EXCLUDED.deadline_at)`,
			tc.execID, ps.nodeID, ps.status, tc.seq, dlArg); err != nil {
			return err
		}
	}

	if treeStatus != "running" {
		if _, err := tx.Exec(ctx,
			`UPDATE executions SET status=$2, updated_at=now() WHERE id=$1`,
			tc.execID, treeStatus); err != nil {
			return err
		}
	}
	if _, err := tx.Exec(ctx,
		`UPDATE ticks SET status='completed', tree_status=$2, ended_at=now() WHERE id=$1`,
		tc.tickID, treeStatus); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

// dedupePending keeps only the last status recorded per node this tick.
func dedupePending(in []setState) []setState {
	last := map[string]int{}
	for i, s := range in {
		last[s.nodeID] = i
	}
	keep := make([]setState, 0, len(last))
	for i, s := range in {
		if last[s.nodeID] == i {
			keep = append(keep, s)
		}
	}
	return keep
}

func (tc *tickContext) nodeMapSnapshot() map[string]string {
	out := make(map[string]string, len(tc.states))
	for id, r := range tc.states {
		out[id] = r.Status
	}
	return out
}

func (tc *tickContext) callMapSnapshot() map[string]CallDTO {
	out := make(map[string]CallDTO, len(tc.calls))
	for id, c := range tc.calls {
		out[id] = CallDTO{
			CallID:        c.ID,
			Stub:          c.Stub,
			Attempt:       c.Attempt,
			Status:        c.Status,
			NonIdempotent: c.NonIdempotent,
			Result:        c.Result,
			Error:         c.Error,
		}
	}
	return out
}

// evalNode evaluates one node against latched state, returning its status.
// Terminal statuses in node_states are latched and returned without
// re-executing anything; that latch is the mechanism that prevents successful
// non-idempotent actions from ever running again.
func evalNode(tc *tickContext, n *model.Node) string {
	if s := tc.nodeStatus(n.ID); isTerminal(s) {
		return s
	}
	if err := tc.check(); err != nil {
		return model.StatusRunning // caller observes interruption
	}
	switch n.Kind {
	case model.KindRoot, model.KindSequence:
		return evalSequenceLike(tc, n, false)
	case model.KindFallback:
		return evalSequenceLike(tc, n, true)
	case model.KindParallel:
		return evalParallel(tc, n)
	case model.KindTimeout:
		return evalTimeout(tc, n)
	case model.KindInverter:
		s := evalNode(tc, n.Child)
		switch s {
		case model.StatusSuccess:
			return tc.latch(n.ID, model.StatusFailure)
		case model.StatusFailure, model.StatusCanceled:
			return tc.latch(n.ID, model.StatusSuccess)
		default:
			tc.setNode(n.ID, model.StatusRunning)
			return model.StatusRunning
		}
	case model.KindSucceeder:
		s := evalNode(tc, n.Child)
		if s == model.StatusRunning {
			tc.setNode(n.ID, model.StatusRunning)
			return model.StatusRunning
		}
		return tc.latch(n.ID, model.StatusSuccess)
	case model.KindAction:
		return evalAction(tc, n)
	default:
		// Validated at publish; defensive.
		return tc.latch(n.ID, model.StatusFailure)
	}
}

// evalSequenceLike handles Sequence (fallback=false) and Fallback
// (fallback=true). Both walk children left-to-right skipping latched
// terminals:
//
//   - Sequence: success moves to the next child; first failure/canceled
//     fails the node; running keeps it running.
//   - Fallback: failure moves to the next child; first success succeeds the
//     node; running keeps it running.
func evalSequenceLike(tc *tickContext, n *model.Node, fallback bool) string {
	for _, c := range n.Children {
		s := evalNode(tc, c)
		if err := tc.check(); err != nil {
			return model.StatusRunning
		}
		if s == model.StatusRunning {
			tc.setNode(n.ID, model.StatusRunning)
			return model.StatusRunning
		}
		if fallback {
			if s == model.StatusSuccess {
				return tc.latch(n.ID, model.StatusSuccess)
			}
			// failure or canceled: try next child
			continue
		}
		// sequence
		if s == model.StatusFailure || s == model.StatusCanceled {
			return tc.latch(n.ID, model.StatusFailure)
		}
		// success: next child
	}
	if fallback {
		return tc.latch(n.ID, model.StatusFailure)
	}
	return tc.latch(n.ID, model.StatusSuccess)
}

// evalParallel ticks every child, counting terminal outcomes against the
// configured thresholds. Whichever threshold is reached first decides; the
// still-running siblings are then canceled (success race included).
func evalParallel(tc *tickContext, n *model.Node) string {
	succThr, failThr := n.ParallelThresholds(len(n.Children))
	succ, fail, running := 0, 0, []*model.Node{}
	statuses := map[string]string{}
	for _, c := range n.Children {
		s := evalNode(tc, c)
		if err := tc.check(); err != nil {
			return model.StatusRunning
		}
		statuses[c.ID] = s
		switch s {
		case model.StatusSuccess:
			succ++
		case model.StatusFailure, model.StatusCanceled:
			fail++
		case model.StatusRunning:
			running = append(running, c)
		}
	}
	if succ >= succThr {
		if err := tc.check(); err != nil {
			return model.StatusRunning
		}
		tc.cancelSiblings(n, running)
		return tc.latch(n.ID, model.StatusSuccess)
	}
	if fail >= failThr {
		if err := tc.check(); err != nil {
			return model.StatusRunning
		}
		tc.cancelSiblings(n, running)
		return tc.latch(n.ID, model.StatusFailure)
	}
	tc.setNode(n.ID, model.StatusRunning)
	return model.StatusRunning
}

// evalTimeout enforces a wall-clock deadline over its single child. The
// deadline is set on the first tick and persisted on the timeout node state;
// while the deadline holds the child is evaluated normally; once elapsed the
// child subtree is canceled and the timeout node latches failure.
func evalTimeout(tc *tickContext, n *model.Node) string {
	state := tc.states[n.ID]
	var deadline time.Time
	if state != nil && state.DeadlineAt != nil && state.DeadlineAt.Valid {
		deadline = state.DeadlineAt.Time
	} else {
		deadline = tc.engine.now().Add(time.Duration(n.MS) * time.Millisecond)
		tc.setDeadline(n.ID, deadline)
	}
	if !tc.engine.now().Before(deadline) {
		if err := tc.check(); err != nil {
			return model.StatusRunning
		}
		// Budget exhausted: cancel whatever is running underneath.
		tc.cancelSubtree(n.Child)
		return tc.latch(n.ID, model.StatusFailure)
	}
	s := evalNode(tc, n.Child)
	if err := tc.check(); err != nil {
		return model.StatusRunning
	}
	switch s {
	case model.StatusSuccess:
		return tc.latch(n.ID, model.StatusSuccess)
	case model.StatusFailure, model.StatusCanceled:
		return tc.latch(n.ID, model.StatusFailure)
	default:
		tc.setNode(n.ID, model.StatusRunning)
		return model.StatusRunning
	}
}

func isTerminal(s string) bool {
	return s == model.StatusSuccess || s == model.StatusFailure || s == model.StatusCanceled
}

// latch records a terminal node status.
func (tc *tickContext) latch(id, status string) string {
	tc.setNode(id, status)
	return status
}

var _ = pgx.ErrNoRows
var _ = stub.KindSync
var _ = json.RawMessage(nil)
