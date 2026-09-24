package engine

import (
	"context"
	"encoding/json"

	"bt/internal/model"
)

// PublishTree validates and publishes a definition, returning the immutable
// tree id, name-scoped version and content hash.
func (e *Engine) PublishTree(ctx context.Context, t *model.Tree) (*PublishResult, error) {
	treeID := randomID("tree")
	row, err := e.st.PublishTree(ctx, t, treeID)
	if err != nil {
		return nil, err
	}
	return &PublishResult{
		TreeID:  row.ID,
		Name:    row.Name,
		Version: row.Version,
		Hash:    row.Hash,
	}, nil
}

// PublishResult is returned by PublishTree.
type PublishResult struct {
	TreeID  string `json:"tree_id"`
	Name    string `json:"name"`
	Version int    `json:"version"`
	Hash    string `json:"hash"`
}

// StartExecution creates a new execution bound to a published tree id and
// version (version 0 = latest).
func (e *Engine) StartExecution(ctx context.Context, treeID string, version int) (string, int, error) {
	execID := randomID("exec")
	exec, _, err := e.st.CreateExecution(ctx, execID, treeID, version)
	if err != nil {
		return "", 0, err
	}
	// Pre-create the runtime context so cancel works even before first tick.
	e.runtimeFor(execID)
	return exec.ID, exec.TreeVersion, nil
}

// Snapshot returns the complete externally visible state of an execution.
func (e *Engine) Snapshot(ctx context.Context, execID string) (*SnapshotDTO, error) {
	exec, treeRow, err := e.st.ExecutionTree(ctx, execID)
	if err != nil {
		return nil, err
	}
	states, calls, err := e.st.LoadSnapshot(ctx, execID)
	if err != nil {
		return nil, err
	}
	dto := &SnapshotDTO{
		ExecutionID: execID,
		TreeID:      treeRow.ID,
		TreeVersion: exec.TreeVersion,
		TreeHash:    treeRow.Hash,
		Status:      exec.Status,
		Nodes:       map[string]string{},
		Calls:       map[string]CallDTO{},
	}
	for id, s := range states {
		dto.Nodes[id] = s.Status
	}
	for id, c := range calls {
		dto.Calls[id] = CallDTO{
			CallID:        c.ID,
			Stub:          c.Stub,
			Attempt:       c.Attempt,
			Status:        c.Status,
			NonIdempotent: c.NonIdempotent,
			Result:        json.RawMessage(c.Result),
			Error:         c.Error,
		}
	}
	ticks, err := e.st.ListTicks(ctx, execID)
	if err == nil && len(ticks) > 0 {
		last := ticks[len(ticks)-1]
		dto.LastTick = &TickResult{
			TickID: last.ID, Seq: last.Seq, Status: last.Status,
			TreeStatus: treeStatusOrRunning(last.TreeStatus, exec.Status),
			Nodes:      dto.Nodes, Calls: dto.Calls,
		}
	}
	return dto, nil
}

func treeStatusOrRunning(s *string, execStatus string) string {
	if s != nil {
		return *s
	}
	return execStatus
}

// TreeMeta is metadata of a published tree version.
type TreeMeta struct {
	TreeID  string `json:"tree_id"`
	Name    string `json:"name"`
	Version int    `json:"version"`
	Hash    string `json:"hash"`
}

// TreeMeta returns metadata for a published tree id/version (version <=0
// selects the latest version of that id lineage).
func (e *Engine) TreeMeta(ctx context.Context, treeID string, version int) (*TreeMeta, error) {
	row, err := e.st.GetTree(ctx, treeID, version)
	if err != nil {
		return nil, err
	}
	return &TreeMeta{TreeID: row.ID, Name: row.Name, Version: row.Version, Hash: row.Hash}, nil
}

// StubInvocationCount exposes the physical execution count of an action
// (used by tests/acceptance to prove no duplicate side effects).
func (e *Engine) StubInvocationCount(ctx context.Context, execID, nodeID string) (int, error) {
	return e.st.CountStubInvocations(ctx, execID, nodeID)
}
