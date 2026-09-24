package engine

import (
	"context"
	"encoding/json"
	"log"
	"time"

	"github.com/google/uuid"

	"resumable-bt/internal/store"
	"resumable-bt/internal/tree"
)

func jsonUnmarshal(raw []byte, target any) error { return json.Unmarshal(raw, target) }
func unixMSToTime(ms int64) time.Time            { return time.UnixMilli(ms) }

// Snapshot returns the full persisted state of an execution plus the earliest
// pending timeout deadline (so a freshly started server can re-arm timers).
func (e *Engine) Snapshot(ctx context.Context, id uuid.UUID) (Snapshot, error) {
	exec, err := e.st.GetExecution(ctx, id)
	if err != nil {
		return Snapshot{}, err
	}
	nss, err := e.st.ListNodeStates(ctx, id)
	if err != nil {
		return Snapshot{}, err
	}
	invs, err := e.st.ListInvocations(ctx, id)
	if err != nil {
		return Snapshot{}, err
	}
	ticks, err := e.st.ListTicks(ctx, id)
	if err != nil {
		return Snapshot{}, err
	}

	snap := Snapshot{Execution: exec}
	for _, ns := range nss {
		snap.NodeStates = append(snap.NodeStates, NodeStateView{
			NodeID: ns.NodeID, Status: ns.Status, Detail: ns.Detail, UpdatedTick: ns.UpdatedTick,
		})
	}
	for _, iv := range invs {
		snap.Invocations = append(snap.Invocations, InvocationView{
			NodeID: iv.NodeID, StableKey: iv.StableKey, Action: iv.Action,
			Idempotent: iv.Idempotent, Params: iv.Params, Status: iv.Status,
			Attempt: iv.Attempt, Result: iv.Result, Error: iv.Err,
			Dispatches: iv.Dispatches, CreatedTick: iv.CreatedTick, UpdatedTick: iv.UpdatedTick,
		})
	}
	snap.Ticks = ticks

	ver, err := e.st.GetVersion(ctx, exec.TreeName, exec.TreeVersion)
	if err == nil {
		if def, perr := tree.ParseDefinition(ver.Definition); perr == nil {
			snap.NextDueUnixMS = earliestDeadline(def, nss)
		}
	}
	return snap, nil
}

func earliestDeadline(def *tree.Definition, nss []store.NodeStateRow) int64 {
	runningTimeout := map[string]bool{}
	status := map[string]string{}
	for _, ns := range nss {
		status[ns.NodeID] = ns.Status
	}
	var best int64
	for id, n := range def.Nodes {
		if n.Kind != tree.KindTimeout || status[id] != string(tree.Running) {
			continue
		}
		runningTimeout[id] = true
		var d timeoutDetail
		for _, ns := range nss {
			if ns.NodeID == id {
				_ = jsonUnmarshal(ns.Detail, &d)
			}
		}
		if d.DeadlineUnixMS > 0 && (best == 0 || d.DeadlineUnixMS < best) {
			best = d.DeadlineUnixMS
		}
	}
	return best
}

// Recover re-drives invocations orphaned by a previous process shutdown:
// every pending/running row of a still-running execution is claimed again on
// a new attempt (bumping its epoch so any zombie report is rejected), and
// timeout timers are re-armed. Succeeded actions are deliberately NOT touched:
// their rows are terminal and the walker reuses them without re-execution.
func (e *Engine) Recover(ctx context.Context) error {
	orphans, err := e.st.OrphanedActiveInvocations(ctx)
	if err != nil {
		return err
	}
	byExec := map[uuid.UUID][]store.InvocationRow{}
	for _, o := range orphans {
		byExec[o.ExecID] = append(byExec[o.ExecID], o.Row)
	}
	for execID, rows := range byExec {
		exec, err := e.st.GetExecution(ctx, execID)
		if err != nil {
			continue
		}
		if exec.Status != store.StatusRunning {
			continue
		}
		for _, row := range rows {
			e.runner.dispatch(e.rootCtx, execID, row)
		}
		// Re-arm timeout timers from persisted deadlines, then run one tick so
		// any already-due timeout fires immediately.
		snap, err := e.Snapshot(ctx, execID)
		if err != nil {
			log.Printf("recover snapshot %s: %v", execID, err)
			continue
		}
		if snap.NextDueUnixMS > 0 {
			e.armTimer(execID, unixMSToTime(snap.NextDueUnixMS))
		}
		e.enqueueTick(execID)
	}
	return nil
}
