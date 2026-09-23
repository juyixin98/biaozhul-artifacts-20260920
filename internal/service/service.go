// Package service contains all CostLens business logic.
//
// # Concurrency model
//
// Every mutating pipeline (import and full rebuild) takes one transaction-level
// advisory lock (pg_advisory_xact_lock) for the entire derived-data pipeline.
// Imports and rebuilds therefore serialize against each other:
//
//   - Two concurrent imports of the same bill cannot double-aggregate: the
//     second waits, then sees the first's committed rows and reports them as
//     duplicates (no costs inserted, totals untouched).
//   - A rebuild never loses or double-counts rows imported concurrently: the
//     import blocks until rebuild commits, then recomputes on the rebuilt data.
//
// The costs UNIQUE(account_id, resource_id, cost_date) constraint remains the
// last line of defense.
package service

import (
	"context"
	"errors"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// Service executes business operations against the database pool.
type Service struct {
	pool *pgxpool.Pool
}

func New(pool *pgxpool.Pool) *Service { return &Service{pool: pool} }

// ErrNotFound is returned for scoped lookups that match nothing.
var ErrNotFound = errors.New("not found")

// ConflictError is a same-key/different-content bill collision.
type ConflictError struct {
	Line int
	Key  string
}

func (e *ConflictError) Error() string {
	return "conflicting bill line for key " + e.Key
}

// ValidationError carries a 1-based CSV line number for any whole-batch failure.
type ValidationError struct {
	Line    int
	Code    string
	Message string
}

func (e *ValidationError) Error() string { return e.Message }

const advisoryLockKey int64 = 0xC0571E5 // arbitrary "COSTL" key

// transact runs fn inside one transaction.
func (s *Service) transact(ctx context.Context, fn func(pgx.Tx) error) error {
	conn, err := s.pool.Acquire(ctx)
	if err != nil {
		return err
	}
	defer conn.Release()
	tx, err := conn.Begin(ctx)
	if err != nil {
		return err
	}
	if err := fn(tx); err != nil {
		_ = tx.Rollback(ctx)
		return err
	}
	return tx.Commit(ctx)
}
