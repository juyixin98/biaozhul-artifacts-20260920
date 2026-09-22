package server

import (
	"context"
	"errors"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"

	sqlcgen "dams.local/dams/internal/db/sqlc"
)

// errConflict marks a deterministic 409-class failure (duplicate id with
// different content). It is distinct from transient failures, which are
// retried.
type conflictError struct {
	message string
	detail  any
}

func (e *conflictError) Error() string { return e.message }

// runTx executes fn inside a transaction at READ COMMITTED.
//
// Why READ COMMITTED (rather than SERIALIZABLE): all cross-statement
// serialization requirements are enforced by ordered locks inside the
// transaction — the audit chain's per-org advisory lock, the alerts table's
// optimistic version predicate, and UNIQUE constraints for event/alert
// dedup. Under SERIALIZABLE the transaction snapshot is fixed at its first
// read, so a transaction that waits on the audit advisory lock then reads a
// stale chain tip and collides on the primary key (40001 would not fire for
// an advisory lock alone). READ COMMITTED gives each statement a fresh
// snapshot after the lock, which is exactly the semantics the chain needs.
//
// We still transparently retry serialization failures (40001) and deadlocks
// (40P01), both of which can be raised by the database in concurrent
// execution. Any other error from fn rolls the whole transaction back.
func runTx(
	ctx context.Context,
	pool *pgxpool.Pool,
	fn func(q *sqlcgen.Queries, tx pgx.Tx) error,
) error {
	const maxAttempts = 20
	var lastErr error
	for attempt := 0; attempt < maxAttempts; attempt++ {
		if attempt > 0 {
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(time.Duration(attempt) * time.Millisecond):
			}
		}
		lastErr = func() error {
			tx, err := pool.Begin(ctx)
			if err != nil {
				return err
			}
			defer tx.Rollback(ctx)
			q := sqlcgen.New(tx)
			if err := fn(q, tx); err != nil {
				return err
			}
			return tx.Commit(ctx)
		}()
		if lastErr == nil {
			return nil
		}
		if !isRetryable(lastErr) {
			return lastErr
		}
	}
	return lastErr
}

func isRetryable(err error) bool {
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) {
		switch pgErr.Code {
		case "40001", // serialization_failure
			"40P01", // deadlock_detected
			"55P03": // lock_not_available (defensive; we never set NOWAIT)
			return true
		}
	}
	return false
}
