package engine

import (
	"context"
	"encoding/json"
	"errors"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgtype"

	"bt/internal/model"
	"bt/internal/store"
	"bt/internal/stub"
)

var (
	errCallLatchChanged = errors.New("action call latch changed concurrently")
)

// pgtypeTimestamptz is a short alias used while building the tick context.
type pgtypeTimestamptz = pgtype.Timestamptz

// evalAction drives one action node against its action_calls latch row.
//
// State machine of the call row:
//
//   - no row / interrupted / failure / canceled -> (re)launch a physical
//     attempt with attempt+1. A failed attempt may be retried by a later
//     tick (e.g. the next Fallback pass after other branches ran); only
//     *success* is the dedup latch.
//   - running -> an attempt is physically in flight: return running. On a
//     fresh process the startup reaper converted such rows to interrupted,
//     so a running row always has a live watcher in this process.
//   - success -> return success WITHOUT invoking the stub: this is the
//     guarantee that successful non-idempotent actions never repeat, across
//     ticks and across restarts.
func evalAction(tc *tickContext, n *model.Node) string {
	if err := tc.check(); err != nil {
		return model.StatusRunning
	}
	call := tc.calls[n.ID]

	if call != nil {
		// A 'running' row carried over from the tick snapshot may have been
		// settled asynchronously between ticks (the watcher commits the
		// result to the latch independently). Attempts started by THIS tick
		// are tracked in tc.launched and never refreshed (their watcher
		// cannot finish before the launching tick returns control here).
		if _, mine := tc.launched[n.ID]; !mine && call.Status == "running" {
			fresh, err := tc.engine.st.GetCall(tc.ctx, tc.execID, n.ID)
			if err == nil && fresh != nil {
				tc.calls[n.ID] = fresh
				call = fresh
			}
		}
		switch call.Status {
		case "success":
			tc.setNode(n.ID, model.StatusSuccess)
			return model.StatusSuccess
		case "failure":
			tc.setNode(n.ID, model.StatusFailure)
			return model.StatusFailure
		case "running":
			// Live in-process watcher (rows from a dead process were reaped
			// to 'interrupted' at startup).
			tc.setNode(n.ID, model.StatusRunning)
			return model.StatusRunning
		case "canceled", "interrupted":
			// fall through to relaunch with a new attempt
		default:
			// defensive: relaunch
		}
	}

	status, err := tc.launch(n, call)
	if err != nil {
		if errors.Is(err, errCallLatchChanged) {
			// The latch moved concurrently (watcher/abort); adopt it.
			fresh, ferr := tc.engine.st.GetCall(tc.ctx, tc.execID, n.ID)
			if ferr == nil && fresh != nil {
				tc.calls[n.ID] = fresh
				switch fresh.Status {
				case "success":
					tc.setNode(n.ID, model.StatusSuccess)
					return model.StatusSuccess
				case "failure":
					tc.setNode(n.ID, model.StatusFailure)
					return model.StatusFailure
				case "running":
					tc.setNode(n.ID, model.StatusRunning)
					return model.StatusRunning
				}
			}
		}
		// Persistent failures to record the launch are surfaced as action
		// failure so the tree can react (e.g. via Fallback).
		tc.recordLaunchError(n, err.Error())
		return tc.latch(n.ID, model.StatusFailure)
	}
	if a, ok := tc.calls[n.ID]; ok {
		tc.launched[n.ID] = a.Attempt
	}
	tc.setNode(n.ID, status)
	return status
}

// launch physically starts one stub attempt as an independent saga
// transaction. The 'running' claim is committed BEFORE the stub runs, which
// is what makes a crashed or interrupted tick safe: the claim survives, the
// startup reaper (or next tick) converts it to interrupted, and because it
// never succeeded the action is allowed to run again — success is the only
// outcome that suppresses re-execution.
func (tc *tickContext) launch(n *model.Node, prev *store.CallRow) (string, error) {
	attempt := 1
	callID := randomID("call")
	if prev != nil {
		attempt = prev.Attempt + 1
		callID = prev.ID // same latch identity, new physical attempt
	}

	args := json.RawMessage(nil)
	if n.Args != nil {
		args = *n.Args
	}

	kind, ok := tc.engine.rg.Lookup(n.Stub)
	if !ok {
		// Unknown stub: record the claim as failure directly, no invocation.
		if err := tc.upsertRunning(n, callID, attempt); err != nil {
			return "", err
		}
		res := stub.Result{Status: "failure", Err: "unknown stub: " + n.Stub}
		if err := tc.finishCall(n.ID, attempt, res); err != nil {
			return "", err
		}
		row := tc.calls[n.ID]
		row.Status = "failure"
		row.Error = res.Err
		return model.StatusFailure, nil
	}

	// Sync attempts derive from tc.ctx (die with the tick); async attempts
	// derive from rt.ctx (survive between ticks, die with abort / subtree
	// cancel). Both are independently cancelable via the attempt handle.
	if kind == stub.KindSync {
		// A synchronous attempt lives and dies with its tick: tick
		// interruption (request cancel) and subtree/abort cancels both reach
		// it through tc.ctx; the handle adds per-attempt cancellation.
		attemptCtx, cancelAttempt := context.WithCancel(tc.ctx)
		handle := &attemptHandle{nodeID: n.ID, attempt: attempt, cancel: cancelAttempt}

		// Commit the claim first.
		if err := tc.upsertRunning(n, callID, attempt); err != nil {
			cancelAttempt()
			return "", err
		}
		tc.rt.registerAttempt(handle)

		invID, err := tc.writeInvocationStart(n, callID, attempt, "sync")
		if err != nil {
			cancelAttempt()
			return "", err
		}
		res := tc.engine.rg.RunSync(attemptCtx, n.Stub, args)

		if errors.Is(attemptCtx.Err(), context.Canceled) ||
			errors.Is(tc.ctx.Err(), context.Canceled) ||
			errors.Is(tc.rt.ctx.Err(), context.Canceled) {
			// Physical attempt canceled mid-flight (tick interrupt / subtree
			// cancel / abort). No successful side effect is trusted: mark
			// interrupted so a future tick may run it again.
			_ = tc.writeInvocationEnd(invID, "")
			_ = tc.interruptCall(n.ID, attempt)
			tc.rt.unregisterAttempt(n.ID, handle)
			cancelAttempt()
			row := tc.calls[n.ID]
			row.Status = "interrupted"
			return model.StatusRunning, nil
		}
		if err := tc.writeInvocationEnd(invID, res.Status); err != nil {
			cancelAttempt()
			return "", err
		}
		if err := tc.finishCall(n.ID, attempt, res); err != nil {
			cancelAttempt()
			return "", err
		}
		tc.rt.unregisterAttempt(n.ID, handle)
		cancelAttempt()
		row := tc.calls[n.ID]
		row.Status = res.Status
		row.Result = res.Output
		row.Error = res.Err
		if res.Status == "success" {
			return model.StatusSuccess, nil
		}
		return model.StatusFailure, nil
	}

	// Async: commit claim, start watcher, return running immediately. The
	// attempt outlives the tick; it is canceled only by abort (rt.ctx), a
	// parent subtree cancel, or a relaunch (handle replaced).
	attemptCtx, cancelAttempt := context.WithCancel(tc.rt.ctx)
	handle := &attemptHandle{nodeID: n.ID, attempt: attempt, cancel: cancelAttempt}

	if err := tc.upsertRunning(n, callID, attempt); err != nil {
		cancelAttempt()
		return "", err
	}
	ch, exists := tc.engine.rg.RunAsync(attemptCtx, n.Stub, args)
	if !exists {
		cancelAttempt()
		return "", errors.New("stub registry inconsistency for " + n.Stub)
	}
	tc.rt.registerAttempt(handle)

	// Audit row for the physical async attempt.
	invID, err := tc.writeInvocationStart(n, callID, attempt, "async")
	if err != nil {
		// Non-fatal for the latch, but surface it.
		cancelAttempt()
		return "", err
	}
	tc.watch(n, attempt, invID, ch, handle)
	return model.StatusRunning, nil
}

// watch consumes the single async result and fences it against the persisted
// attempt number. A stale result (canceled timeout, abort, relaunch) is
// dropped and never touches tree state. The tree is NOT ticked here.
func (tc *tickContext) watch(n *model.Node, attempt int, invID int64, ch <-chan stub.Result, handle *attemptHandle) {
	go func() {
		select {
		case res, ok := <-ch:
			if !ok {
				// Context fired before completion: close out audit, leave the
				// row as the canceler set it (canceled/interrupted).
				_ = tc.writeInvocationEnd(invID, "")
				return
			}
			_ = tc.writeInvocationEnd(invID, res.Status)
			applied, err := tc.finishCallIfAttempt(n.ID, attempt, res)
			if err != nil {
				return
			}
			if !applied {
				// Late result: fence rejected it, nothing to do.
				return
			}
			// Only the latch row changes; the tree advances on next tick.
		case <-time.After(24 * time.Hour):
			// Defensive bound; real cancellation comes via attempt ctx which
			// closes ch.
			return
		}
		_ = n
	}()
}

func (rt *execRuntime) registerAttempt(h *attemptHandle) {
	rt.mu.Lock()
	defer rt.mu.Unlock()
	if old, ok := rt.attempts[h.nodeID]; ok {
		old.cancel() // should not happen under advisory lock; defensive
	}
	rt.attempts[h.nodeID] = h
}

func (rt *execRuntime) unregisterAttempt(nodeID string, h *attemptHandle) {
	rt.mu.Lock()
	defer rt.mu.Unlock()
	if cur, ok := rt.attempts[nodeID]; ok && cur == h {
		delete(rt.attempts, nodeID)
	}
}

// cancelAttempt cancels a single live attempt and marks the persisted call
// (fenced by attempt) as given status. Used by subtree cancellation. It
// reports whether the call was actually still cancelable: a result that
// committed in the same instant (success/failure) wins and is not overwritten.
func (tc *tickContext) cancelAttempt(nodeID, status string) bool {
	tc.rt.mu.Lock()
	h := tc.rt.attempts[nodeID]
	tc.rt.mu.Unlock()
	if h != nil {
		h.cancel()
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	attempt := 0
	if h != nil {
		attempt = h.attempt
	} else if c := tc.calls[nodeID]; c != nil {
		attempt = c.Attempt
	}
	if attempt <= 0 {
		return false
	}
	var ct pgconn.CommandTag
	err := tc.sagaTx(ctx, func(tx pgx.Tx) error {
		var e error
		ct, e = tx.Exec(ctx,
			`UPDATE action_calls SET status=$3, updated_seq=$4
			 WHERE execution_id=$1 AND node_id=$2 AND status IN ('running','interrupted') AND attempt=$5`,
			tc.execID, nodeID, status, tc.seq, attempt)
		return e
	})
	if err != nil {
		return false
	}
	applied := ct.RowsAffected() > 0
	if applied {
		if c := tc.calls[nodeID]; c != nil &&
			(c.Status == "running" || c.Status == "interrupted") {
			c.Status = status
		}
	}
	return applied
}

// markTickInterruptedAttempts is called when the tick request is canceled
// mid-evaluation. Only attempts physically started by THIS tick are
// canceled/marked interrupted; asynchronous attempts launched by earlier
// ticks keep running legitimately and their results remain valid for later
// ticks.
func (e *Engine) markTickInterruptedAttempts(tc *tickContext) {
	for nodeID, attempt := range tc.launched {
		c := tc.calls[nodeID]
		if c == nil || c.Status != "running" {
			continue
		}
		tc.rt.mu.Lock()
		h := tc.rt.attempts[nodeID]
		tc.rt.mu.Unlock()
		if h != nil && h.attempt == attempt {
			h.cancel()
		}
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		_, _ = e.st.DB().Exec(ctx,
			`UPDATE action_calls SET status='interrupted', updated_seq=$3
			 WHERE execution_id=$1 AND node_id=$2 AND status='running' AND attempt=$4`,
			tc.execID, nodeID, tc.seq, attempt)
		cancel()
		c.Status = "interrupted"
	}
}

// cancelSiblings cancels still-running parallel children after a threshold
// decided the node. Action children are physically canceled; composite
// running children are traversed recursively.
func (tc *tickContext) cancelSiblings(par *model.Node, running []*model.Node) {
	for _, c := range running {
		tc.cancelSubtree(c)
	}
}

// cancelSubtree marks every currently running node in a subtree as canceled
// and physically cancels running action attempts. Used when a parent decides
// (parallel threshold / timeout failure) and must cancel its running
// remainder.
func (tc *tickContext) cancelSubtree(n *model.Node) {
	s := tc.nodeStatus(n.ID)
	if s == model.StatusCanceled {
		return
	}
	if s == model.StatusRunning || s == "" {
		if n.Kind == model.KindAction {
			// An async watcher may have committed a result in the microseconds
			// between this node being evaluated as running and the parent's
			// decision. Re-read the latch: a terminal result wins and is
			// reported truthfully; only a still-running call is canceled.
			if _, mine := tc.launched[n.ID]; !mine {
				if fresh, err := tc.engine.st.GetCall(tc.ctx, tc.execID, n.ID); err == nil && fresh != nil {
					tc.calls[n.ID] = fresh
					switch fresh.Status {
					case "success":
						tc.setNode(n.ID, model.StatusSuccess)
						return
					case "failure":
						tc.setNode(n.ID, model.StatusFailure)
						return
					}
				}
			}
			if tc.cancelAttempt(n.ID, "canceled") {
				tc.canceled[n.ID] = true
				tc.setNode(n.ID, model.StatusCanceled)
			} else if c := tc.calls[n.ID]; c != nil {
				// Lost the race with the watcher: reflect the true outcome.
				tc.setNode(n.ID, c.Status)
			}
			return
		}
		for _, c := range n.Children {
			tc.cancelSubtree(c)
		}
		if n.Child != nil {
			tc.cancelSubtree(n.Child)
		}
		tc.canceled[n.ID] = true
		tc.setNode(n.ID, model.StatusCanceled)
	}
}

func (tc *tickContext) recordLaunchError(n *model.Node, msg string) {
	// evalAction already latched failure; keep error on the in-memory call.
	if c := tc.calls[n.ID]; c != nil {
		c.Status = "failure"
		c.Error = msg
	}
}

// --- saga SQL (independent transactions, committed immediately) -------------

func (tc *tickContext) sagaTx(ctx context.Context, fn func(pgx.Tx) error) error {
	pool := tc.engine.st.DB()
	tx, err := pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	if err := fn(tx); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func (tc *tickContext) upsertRunning(n *model.Node, callID string, attempt int) error {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	err := tc.sagaTx(ctx, func(tx pgx.Tx) error {
		ct, err := tx.Exec(ctx, `
			INSERT INTO action_calls(execution_id, node_id, id, stub, non_idempotent,
			                         attempt, status, launched_seq, updated_seq)
			VALUES ($1,$2,$3,$4,$5,$6,'running',$7,$7)
			ON CONFLICT (execution_id, node_id) DO UPDATE
			  SET id=EXCLUDED.id, stub=EXCLUDED.stub,
			      non_idempotent=EXCLUDED.non_idempotent,
			      attempt=EXCLUDED.attempt, status='running',
			      result=NULL, error='',
			      launched_seq=EXCLUDED.launched_seq, updated_seq=EXCLUDED.updated_seq
			WHERE action_calls.attempt = EXCLUDED.attempt - 1
			  AND action_calls.status <> 'success'`,
			tc.execID, n.ID, callID, n.Stub, n.NonIdempotent, attempt, tc.seq)
		if err != nil {
			return err
		}
		if ct.RowsAffected() == 0 {
			// Concurrent watcher/abort changed the latch; the caller must
			// re-read instead of assuming a running claim.
			return errCallLatchChanged
		}
		return nil
	})
	if err != nil {
		return err
	}
	// Update in-memory row.
	row := &store.CallRow{
		NodeID: n.ID, ID: callID, Stub: n.Stub, NonIdempotent: n.NonIdempotent,
		Attempt: attempt, Status: "running", LaunchedSeq: tc.seq, UpdatedSeq: tc.seq,
	}
	tc.calls[n.ID] = row
	return nil
}

func (tc *tickContext) finishCall(nodeID string, attempt int, res stub.Result) error {
	_, err := tc.finishCallIfAttempt(nodeID, attempt, res)
	return err
}

// finishCallIfAttempt writes the terminal result only if the persisted
// attempt still matches — the fence that rejects late results.
func (tc *tickContext) finishCallIfAttempt(nodeID string, attempt int, res stub.Result) (bool, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	var applied bool
	err := tc.sagaTx(ctx, func(tx pgx.Tx) error {
		ct, err := tx.Exec(ctx, `
			UPDATE action_calls
			   SET status=$3, result=$4, error=$5, updated_seq=$6
			 WHERE execution_id=$1 AND node_id=$2 AND attempt=$7
			   AND status='running'`,
			tc.execID, nodeID, res.Status, rawJSONOrNil(res.Output), res.Err, tc.seq, attempt)
		if err != nil {
			return err
		}
		applied = ct.RowsAffected() > 0
		return nil
	})
	if err != nil {
		return false, err
	}
	if applied {
		if c := tc.calls[nodeID]; c != nil && c.Attempt == attempt {
			c.Status = res.Status
			c.Result = res.Output
			c.Error = res.Err
		}
	}
	return applied, nil
}

// interruptCall marks a canceled in-flight sync attempt as interrupted so it
// can be safely re-attempted (it produced no trusted success).
func (tc *tickContext) interruptCall(nodeID string, attempt int) error {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	return tc.sagaTx(ctx, func(tx pgx.Tx) error {
		_, err := tx.Exec(ctx, `
			UPDATE action_calls
			   SET status='interrupted', updated_seq=$4
			 WHERE execution_id=$1 AND node_id=$2 AND attempt=$3
			   AND status='running'`,
			tc.execID, nodeID, attempt, tc.seq)
		if err != nil {
			return err
		}
		if c := tc.calls[nodeID]; c != nil && c.Attempt == attempt && c.Status == "running" {
			c.Status = "interrupted"
		}
		return nil
	})
}

func (tc *tickContext) writeInvocationStart(n *model.Node, callID string, attempt int, mode string) (int64, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	var id int64
	err := tc.sagaTx(ctx, func(tx pgx.Tx) error {
		return tx.QueryRow(ctx, `
			INSERT INTO stub_invocations(execution_id, node_id, call_id, attempt, stub, mode)
			VALUES ($1,$2,$3,$4,$5,$6) RETURNING id`,
			tc.execID, n.ID, callID, attempt, n.Stub, mode).Scan(&id)
	})
	return id, err
}

func (tc *tickContext) writeInvocationEnd(invID int64, outcome string) error {
	if invID == 0 {
		return nil
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	return tc.sagaTx(ctx, func(tx pgx.Tx) error {
		_, err := tx.Exec(ctx, `
			UPDATE stub_invocations SET ended_at=now(), outcome=$2 WHERE id=$1`,
			invID, outcomeNil(outcome))
		return err
	})
}

func rawJSONOrNil(b json.RawMessage) any {
	if len(b) == 0 {
		return nil
	}
	return b
}

func outcomeNil(s string) any {
	if s == "" {
		return nil
	}
	return s
}
