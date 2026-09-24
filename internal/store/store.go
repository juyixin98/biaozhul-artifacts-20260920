// Package store implements PostgreSQL persistence for published tree
// definitions, executions, tick sequence, node states and the action-call
// dedup latch.
package store

import (
	"context"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgtype"
	"github.com/jackc/pgx/v5/pgxpool"

	"bt/internal/model"
)

//go:embed schema.sql
var schemaSQL string

// ErrNotFound is returned when a referenced row does not exist.
var ErrNotFound = errors.New("not found")

// ErrAlreadyTerminal is returned when ticking/aborting a finished execution.
var ErrAlreadyTerminal = errors.New("execution already terminal")

// TreeRow is a persisted published tree.
type TreeRow struct {
	ID      string
	Name    string
	Version int
	Hash    string
	Tree    *model.Tree
}

// ExecutionRow is a persisted execution.
type ExecutionRow struct {
	ID          string
	TreeID      string
	TreeVersion int
	Status      string
}

// NodeStateRow is the persisted state of one node.
type NodeStateRow struct {
	NodeID     string
	Status     string
	UpdatedSeq int64
	DeadlineAt *pgtype.Timestamptz // nullable timestamptz
}

// CallRow is the persisted action-call latch row.
type CallRow struct {
	NodeID        string
	ID            string
	Stub          string
	NonIdempotent bool
	Attempt       int
	Status        string
	Result        []byte // raw JSON, nil when none
	Error         string
	LaunchedSeq   int64
	UpdatedSeq    int64
}

// TickRow is the persisted tick record.
type TickRow struct {
	ID         string
	Seq        int64
	Status     string
	TreeStatus *string
}

// Store wraps a pgx pool.
type Store struct {
	pool *pgxpool.Pool
}

// New constructs a Store.
func New(pool *pgxpool.Pool) *Store { return &Store{pool: pool} }

// Migrate applies the embedded schema (idempotent) and records the version.
func (s *Store) Migrate(ctx context.Context) error {
	if _, err := s.pool.Exec(ctx, schemaSQL); err != nil {
		return fmt.Errorf("apply schema: %w", err)
	}
	_, err := s.pool.Exec(ctx,
		`INSERT INTO schema_migrations(version) VALUES (1) ON CONFLICT DO NOTHING`)
	return err
}

// PublishTree validates, content-addresses and publishes a tree definition.
// Publishing the same definition again (same hash) returns the existing row;
// a changed definition under the same name gets a new name-scoped version.
// Definitions are immutable afterwards: callers never write to trees again.
func (s *Store) PublishTree(ctx context.Context, t *model.Tree, treeID string) (*TreeRow, error) {
	if err := t.Validate(); err != nil {
		return nil, err
	}
	hash, err := t.Hash()
	if err != nil {
		return nil, err
	}
	defJSON, err := json.Marshal(t)
	if err != nil {
		return nil, err
	}

	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx)

	// Same content: idempotent publish.
	var existing TreeRow
	var rawDef []byte
	err = tx.QueryRow(ctx,
		`SELECT id, name, version, definition_hash, definition FROM trees WHERE definition_hash = $1`,
		hash).Scan(&existing.ID, &existing.Name, &existing.Version, &existing.Hash, &rawDef)
	switch {
	case err == nil:
		var tr model.Tree
		if err := json.Unmarshal(rawDef, &tr); err != nil {
			return nil, err
		}
		existing.Tree = &tr
		return &existing, tx.Commit(ctx)
	case !errors.Is(err, pgx.ErrNoRows):
		return nil, err
	}

	var version int
	err = tx.QueryRow(ctx,
		`SELECT COALESCE(MAX(version), 0) + 1 FROM trees WHERE name = $1`, t.Name).Scan(&version)
	if err != nil {
		return nil, err
	}
	// A tree id identifies the immutable name lineage: republishing a
	// changed definition under the same name reuses the existing id and gets
	// the next version. A brand-new name keeps the caller-generated id.
	var lineageID *string
	if err := tx.QueryRow(ctx,
		`SELECT id FROM trees WHERE name=$1 ORDER BY version DESC LIMIT 1`, t.Name).
		Scan(&lineageID); err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return nil, err
	}
	finalID := treeID
	if lineageID != nil {
		finalID = *lineageID
	}
	if _, err := tx.Exec(ctx,
		`INSERT INTO trees(id, name, version, definition_hash, definition)
		 VALUES ($1, $2, $3, $4, $5)`,
		finalID, t.Name, version, hash, defJSON); err != nil {
		var pgErr *pgconn.PgError
		if errors.As(err, &pgErr) && pgErr.Code == "23505" {
			return nil, fmt.Errorf("tree %q version conflict (concurrent publish)", t.Name)
		}
		return nil, err
	}
	row := &TreeRow{ID: finalID, Name: t.Name, Version: version, Hash: hash, Tree: t}
	return row, tx.Commit(ctx)
}

// GetTree fetches a published tree by its id and (optionally) version.
// version <= 0 selects the latest version of that tree id's name lineage.
func (s *Store) GetTree(ctx context.Context, treeID string, version int) (*TreeRow, error) {
	var rawDef []byte
	var tr TreeRow
	var err error
	if version > 0 {
		err = s.pool.QueryRow(ctx,
			`SELECT id, name, version, definition_hash, definition
			 FROM trees WHERE id = $1 AND version = $2`,
			treeID, version).
			Scan(&tr.ID, &tr.Name, &tr.Version, &tr.Hash, &rawDef)
	} else {
		err = s.pool.QueryRow(ctx,
			`SELECT id, name, version, definition_hash, definition
			 FROM trees WHERE id = $1
			 ORDER BY version DESC LIMIT 1`,
			treeID).
			Scan(&tr.ID, &tr.Name, &tr.Version, &tr.Hash, &rawDef)
	}
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, err
	}
	var def model.Tree
	if err := json.Unmarshal(rawDef, &def); err != nil {
		return nil, err
	}
	tr.Tree = &def
	return &tr, nil
}

// CreateExecution binds a new execution to a specific published tree id/version.
func (s *Store) CreateExecution(ctx context.Context, execID, treeID string, version int) (*ExecutionRow, *TreeRow, error) {
	tr, err := s.GetTree(ctx, treeID, version)
	if err != nil {
		return nil, nil, err
	}
	_, err = s.pool.Exec(ctx,
		`INSERT INTO executions(id, tree_id, tree_version, status)
		 VALUES ($1, $2, $3, 'running')`,
		execID, tr.ID, tr.Version)
	if err != nil {
		return nil, nil, err
	}
	return &ExecutionRow{ID: execID, TreeID: tr.ID, TreeVersion: tr.Version, Status: "running"}, tr, nil
}

// GetExecution fetches an execution row.
func (s *Store) GetExecution(ctx context.Context, execID string) (*ExecutionRow, error) {
	var e ExecutionRow
	err := s.pool.QueryRow(ctx,
		`SELECT id, tree_id, tree_version, status FROM executions WHERE id = $1`,
		execID).Scan(&e.ID, &e.TreeID, &e.TreeVersion, &e.Status)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, err
	}
	return &e, nil
}

// ExecutionTree loads the tree bound to an execution (the exact published version).
func (s *Store) ExecutionTree(ctx context.Context, execID string) (*ExecutionRow, *TreeRow, error) {
	e, err := s.GetExecution(ctx, execID)
	if err != nil {
		return nil, nil, err
	}
	var rawDef []byte
	var tr TreeRow
	err = s.pool.QueryRow(ctx,
		`SELECT t.id, t.name, t.version, t.definition_hash, t.definition
		 FROM trees t
		 JOIN executions e ON e.tree_id = t.id AND e.tree_version = t.version
		 WHERE e.id = $1`, execID).
		Scan(&tr.ID, &tr.Name, &tr.Version, &tr.Hash, &rawDef)
	if err != nil {
		return nil, nil, err
	}
	var def model.Tree
	if err := json.Unmarshal(rawDef, &def); err != nil {
		return nil, nil, err
	}
	tr.Tree = &def
	return e, &tr, nil
}

// LoadSnapshot loads all node states and action calls of an execution.
func (s *Store) LoadSnapshot(ctx context.Context, execID string) (
	map[string]*NodeStateRow, map[string]*CallRow, error) {
	states := map[string]*NodeStateRow{}
	callByNode := map[string]*CallRow{}

	rows, err := s.pool.Query(ctx,
		`SELECT node_id, status, updated_seq, deadline_at FROM node_states WHERE execution_id = $1`,
		execID)
	if err != nil {
		return nil, nil, err
	}
	for rows.Next() {
		var r NodeStateRow
		if err := rows.Scan(&r.NodeID, &r.Status, &r.UpdatedSeq, &r.DeadlineAt); err != nil {
			rows.Close()
			return nil, nil, err
		}
		states[r.NodeID] = &r
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return nil, nil, err
	}

	crows, err := s.pool.Query(ctx,
		`SELECT node_id, id, stub, non_idempotent, attempt, status, result, error,
		        launched_seq, updated_seq
		 FROM action_calls WHERE execution_id = $1`, execID)
	if err != nil {
		return nil, nil, err
	}
	for crows.Next() {
		var c CallRow
		if err := crows.Scan(&c.NodeID, &c.ID, &c.Stub, &c.NonIdempotent, &c.Attempt,
			&c.Status, &c.Result, &c.Error, &c.LaunchedSeq, &c.UpdatedSeq); err != nil {
			crows.Close()
			return nil, nil, err
		}
		callByNode[c.NodeID] = &c
	}
	crows.Close()
	if err := crows.Err(); err != nil {
		return nil, nil, err
	}
	return states, callByNode, nil
}

// GetCall fetches the current latch row for one action node, or nil if none.
func (s *Store) GetCall(ctx context.Context, execID, nodeID string) (*CallRow, error) {
	var c CallRow
	err := s.pool.QueryRow(ctx,
		`SELECT node_id, id, stub, non_idempotent, attempt, status, result, error,
		        launched_seq, updated_seq
		 FROM action_calls WHERE execution_id = $1 AND node_id = $2`,
		execID, nodeID).Scan(&c.NodeID, &c.ID, &c.Stub, &c.NonIdempotent, &c.Attempt,
		&c.Status, &c.Result, &c.Error, &c.LaunchedSeq, &c.UpdatedSeq)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &c, nil
}

// CountStubInvocations counts physical stub executions for a node.
func (s *Store) CountStubInvocations(ctx context.Context, execID, nodeID string) (int, error) {
	var n int
	if err := s.pool.QueryRow(ctx,
		`SELECT count(*) FROM stub_invocations WHERE execution_id = $1 AND node_id = $2`,
		execID, nodeID).Scan(&n); err != nil {
		return 0, err
	}
	return n, nil
}

// ListTicks returns tick rows for an execution in sequence order.
func (s *Store) ListTicks(ctx context.Context, execID string) ([]TickRow, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT id, seq, status, tree_status FROM ticks WHERE execution_id = $1 ORDER BY seq`,
		execID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []TickRow
	for rows.Next() {
		var t TickRow
		if err := rows.Scan(&t.ID, &t.Seq, &t.Status, &t.TreeStatus); err != nil {
			return nil, err
		}
		out = append(out, t)
	}
	return out, rows.Err()
}

// DB exposes the pool for engine-owned queries.
func (s *Store) DB() *pgxpool.Pool { return s.pool }

// pgx helpers used by the engine live in engine_tx.go to keep SQL next to the
// tick algorithm. The aliases below keep imports localized.
var _ = pgconn.PgError{}
