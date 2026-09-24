// Package store contains PostgreSQL persistence for tree definitions,
// executions, tick records, node states and action invocations.
package store

import (
	"context"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"resumable-bt/internal/tree"
)

//go:embed schema.sql
var schemaSQL string

// ErrNotFound is returned for missing rows.
var ErrNotFound = errors.New("not found")

// ErrConflict is returned on optimistic-concurrency / state mismatches.
var ErrConflict = errors.New("conflict")

// Execution terminal/non-terminal statuses (kept as strings at the edge).
const (
	StatusRunning  = "running"
	StatusSuccess  = "success"
	StatusFailure  = "failure"
	StatusCanceled = "canceled"
)

// Store wraps a connection pool.
type Store struct {
	pool *pgxpool.Pool
}

// Open connects and verifies the pool.
func Open(ctx context.Context, dsn string) (*Store, error) {
	cfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		return nil, fmt.Errorf("parse dsn: %w", err)
	}
	cfg.MaxConns = 10
	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		return nil, fmt.Errorf("connect: %w", err)
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, fmt.Errorf("ping: %w", err)
	}
	return &Store{pool: pool}, nil
}

// Close releases the pool.
func (s *Store) Close() { s.pool.Close() }

// Migrate applies the embedded idempotent schema.
func (s *Store) Migrate(ctx context.Context) error {
	_, err := s.pool.Exec(ctx, schemaSQL)
	return err
}

// Tx runs fn inside a transaction, committing on nil error and rolling back
// otherwise (including when ctx is canceled — the basis of tick interruption).
func (s *Store) Tx(ctx context.Context, fn func(pgx.Tx) error) error {
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return err
	}
	if err := fn(tx); err != nil {
		_ = tx.Rollback(context.Background())
		return err
	}
	return tx.Commit(ctx)
}

// VersionRow is a published immutable tree version.
type VersionRow struct {
	Name        string
	Version     int64
	ContentHash string
	Definition  []byte
	PublishedAt time.Time
}

// PublishTree creates the tree if needed and publishes a new immutable
// version. If the exact content hash already exists for this tree it returns
// the existing row with created=false (idempotent publish).
func (s *Store) PublishTree(ctx context.Context, name string, def *tree.Definition, hash string) (VersionRow, bool, error) {
	var row VersionRow
	created := false
	err := s.Tx(ctx, func(tx pgx.Tx) error {
		if _, err := tx.Exec(ctx,
			`INSERT INTO trees(name) VALUES ($1)
			 ON CONFLICT (name) DO UPDATE SET updated_at = now()`, name); err != nil {
			return err
		}
		var existing int64
		err := tx.QueryRow(ctx,
			`SELECT version FROM tree_versions WHERE name=$1 AND content_hash=$2`,
			name, hash).Scan(&existing)
		switch {
		case err == nil:
			return s.getVersion(ctx, tx, name, existing, &row)
		case !errors.Is(err, pgx.ErrNoRows):
			return err
		}
		raw, err := jsonMarshal(def)
		if err != nil {
			return err
		}
		var next int64
		if err := tx.QueryRow(ctx,
			`SELECT COALESCE(MAX(version),0)+1 FROM tree_versions WHERE name=$1`,
			name).Scan(&next); err != nil {
			return err
		}
		if _, err := tx.Exec(ctx,
			`INSERT INTO tree_versions(name,version,content_hash,definition)
			 VALUES ($1,$2,$3,$4)`,
			name, next, hash, raw); err != nil {
			return err
		}
		created = true
		return s.getVersion(ctx, tx, name, next, &row)
	})
	return row, created, err
}

func (s *Store) getVersion(ctx context.Context, q pgx.Tx, name string, version int64, row *VersionRow) error {
	return q.QueryRow(ctx,
		`SELECT name,version,content_hash,definition,published_at
		 FROM tree_versions WHERE name=$1 AND version=$2`,
		name, version).Scan(&row.Name, &row.Version, &row.ContentHash, &row.Definition, &row.PublishedAt)
}

// GetVersion loads one published version.
func (s *Store) GetVersion(ctx context.Context, name string, version int64) (VersionRow, error) {
	var row VersionRow
	err := s.pool.QueryRow(ctx,
		`SELECT name,version,content_hash,definition,published_at
		 FROM tree_versions WHERE name=$1 AND version=$2`,
		name, version).Scan(&row.Name, &row.Version, &row.ContentHash, &row.Definition, &row.PublishedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return VersionRow{}, ErrNotFound
	}
	return row, err
}

// LatestVersion returns the highest published version number for a tree.
func (s *Store) LatestVersion(ctx context.Context, name string) (int64, error) {
	var v *int64
	if err := s.pool.QueryRow(ctx,
		`SELECT MAX(version) FROM tree_versions WHERE name=$1`, name).Scan(&v); err != nil {
		return 0, err
	}
	if v == nil {
		return 0, ErrNotFound
	}
	return *v, nil
}

// ExecutionRow is the persisted execution header.
type ExecutionRow struct {
	ID          uuid.UUID  `json:"id"`
	TreeName    string     `json:"tree_name"`
	TreeVersion int64      `json:"tree_version"`
	ContentHash string     `json:"content_hash"`
	Status      string     `json:"status"`
	NextTick    int64      `json:"next_tick"`
	CreatedAt   time.Time  `json:"created_at"`
	FinalizedAt *time.Time `json:"finalized_at"`
}

// CreateExecution inserts a new execution bound to an immutable version.
func (s *Store) CreateExecution(ctx context.Context, e ExecutionRow) error {
	return s.Tx(ctx, func(tx pgx.Tx) error {
		_, err := tx.Exec(ctx,
			`INSERT INTO executions(id,tree_name,tree_version,content_hash,status,next_tick)
			 VALUES ($1,$2,$3,$4,$5,1)`,
			e.ID, e.TreeName, e.TreeVersion, e.ContentHash, e.Status)
		return err
	})
}

// GetExecution loads an execution header.
func (s *Store) GetExecution(ctx context.Context, id uuid.UUID) (ExecutionRow, error) {
	var e ExecutionRow
	err := s.pool.QueryRow(ctx,
		`SELECT id,tree_name,tree_version,content_hash,status,next_tick,created_at,finalized_at
		 FROM executions WHERE id=$1`, id).
		Scan(&e.ID, &e.TreeName, &e.TreeVersion, &e.ContentHash, &e.Status,
			&e.NextTick, &e.CreatedAt, &e.FinalizedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return ExecutionRow{}, ErrNotFound
	}
	return e, err
}

// NodeStateRow is a persisted per-node runtime state.
type NodeStateRow struct {
	NodeID      string          `json:"node_id"`
	Status      string          `json:"status"`
	Detail      json.RawMessage `json:"detail"`
	UpdatedTick int64           `json:"updated_tick"`
}

// InvocationRow is one (deduplicated) action dispatch record.
type InvocationRow struct {
	NodeID      string          `json:"node_id"`
	StableKey   string          `json:"stable_key"`
	Action      string          `json:"action"`
	Idempotent  bool            `json:"idempotent"`
	Params      json.RawMessage `json:"params"`
	Status      string          `json:"status"`
	Attempt     int64           `json:"attempt"`
	Result      json.RawMessage `json:"result"`
	Err         string          `json:"error"`
	Dispatches  int64           `json:"dispatches"`
	CreatedTick int64           `json:"created_tick"`
	UpdatedTick int64           `json:"updated_tick"`
}

// ListNodeStates returns all node states for an execution, ordered by node id.
func (s *Store) ListNodeStates(ctx context.Context, execID uuid.UUID) ([]NodeStateRow, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT node_id,status,detail,updated_tick
		 FROM node_states WHERE execution_id=$1 ORDER BY node_id`, execID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	return collectNodeStates(rows)
}

// ListInvocations returns invocation records ordered by node id.
func (s *Store) ListInvocations(ctx context.Context, execID uuid.UUID) ([]InvocationRow, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT node_id,stable_key,action,idempotent,params,status,attempt,
		        COALESCE(result,'null'::jsonb),COALESCE(error,''),dispatches,
		        created_tick,updated_tick
		 FROM invocations WHERE execution_id=$1 ORDER BY node_id`, execID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []InvocationRow
	for rows.Next() {
		var r InvocationRow
		if err := rows.Scan(&r.NodeID, &r.StableKey, &r.Action, &r.Idempotent, &r.Params,
			&r.Status, &r.Attempt, &r.Result, &r.Err, &r.Dispatches,
			&r.CreatedTick, &r.UpdatedTick); err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

func collectNodeStates(rows pgx.Rows) ([]NodeStateRow, error) {
	var out []NodeStateRow
	for rows.Next() {
		var r NodeStateRow
		if err := rows.Scan(&r.NodeID, &r.Status, &r.Detail, &r.UpdatedTick); err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

// TickRow is one persisted tick record.
type TickRow struct {
	Seq           int64      `json:"seq"`
	Status        string     `json:"status"`
	Note          string     `json:"note"`
	StartedAt     time.Time  `json:"started_at"`
	CommittedAt   *time.Time `json:"committed_at"`
	InterruptedAt *time.Time `json:"interrupted_at"`
}

// ListTicks returns tick history for an execution.
func (s *Store) ListTicks(ctx context.Context, execID uuid.UUID) ([]TickRow, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT seq,status,COALESCE(note,''),started_at,committed_at,interrupted_at
		 FROM ticks WHERE execution_id=$1 ORDER BY seq`, execID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []TickRow
	for rows.Next() {
		var r TickRow
		if err := rows.Scan(&r.Seq, &r.Status, &r.Note, &r.StartedAt,
			&r.CommittedAt, &r.InterruptedAt); err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}
