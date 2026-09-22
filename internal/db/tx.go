package db

import (
	"context"
	"errors"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

const maxSerializationRetries = 6

// InTx runs fn inside a serializable transaction. Serializable isolation is
// used for every state-changing use case (edit/submit/approve/claim/report):
// combined with the SQL-level guards (SKIP LOCKED, conditional UPDATE, unique
// indexes), it gives deterministic results under concurrency instead of
// relying on last-write-wins.
//
// On SQLSTATE 40001 (serialization_failure) or 40P01 (deadlock) the whole
// function is retried: PostgreSQL guarantees serializable transactions only
// by aborting conflicting ones, and every use case here is safe to re-run
// because it re-reads fresh state (e.g. an aborted claimant re-issues SKIP
// LOCKED and takes a different task).
func InTx(ctx context.Context, pool *pgxpool.Pool, fn func(pgx.Tx, *Queries) error) error {
	var lastErr error
	for attempt := 0; attempt < maxSerializationRetries; attempt++ {
		lastErr = runOnce(ctx, pool, fn)
		if lastErr == nil || !isRetryable(lastErr) {
			return lastErr
		}
	}
	return lastErr
}

func runOnce(ctx context.Context, pool *pgxpool.Pool, fn func(pgx.Tx, *Queries) error) (err error) {
	tx, err := pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.Serializable})
	if err != nil {
		return err
	}
	defer func() {
		if err != nil {
			_ = tx.Rollback(ctx)
		}
	}()
	if err = fn(tx, New(tx)); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

// InTxRC runs fn in a read-committed transaction with serialization/
// deadlock retries. Used by the claim queue: its correctness comes entirely
// from FOR UPDATE SKIP LOCKED row locks plus a conditional status UPDATE,
// which are correct under READ COMMITTED and avoid the predicate-lock
// conflicts that SERIALIZABLE would create between many simultaneous
// claimants.
func InTxRC(ctx context.Context, pool *pgxpool.Pool, fn func(pgx.Tx, *Queries) error) error {
	var lastErr error
	for attempt := 0; attempt < maxSerializationRetries; attempt++ {
		lastErr = runOnceRC(ctx, pool, fn)
		if lastErr == nil || !isRetryable(lastErr) {
			return lastErr
		}
	}
	return lastErr
}

func runOnceRC(ctx context.Context, pool *pgxpool.Pool, fn func(pgx.Tx, *Queries) error) (err error) {
	tx, err := pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.ReadCommitted})
	if err != nil {
		return err
	}
	defer func() {
		if err != nil {
			_ = tx.Rollback(ctx)
		}
	}()
	if err = fn(tx, New(tx)); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func isRetryable(err error) bool {
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) {
		return pgErr.Code == "40001" || pgErr.Code == "40P01"
	}
	return false
}
