package store

import (
	"context"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// Store wraps a PostgreSQL connection pool.
type Store struct {
	Pool *pgxpool.Pool
}

// New connects to PostgreSQL using the given DSN and verifies the
// connection. The caller owns Close.
func New(ctx context.Context, dsn string) (*Store, error) {
	cfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		return nil, fmt.Errorf("parse database dsn: %w", err)
	}
	cfg.MaxConns = 10
	cfg.MaxConnLifetime = time.Hour
	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		return nil, fmt.Errorf("connect to postgres: %w", err)
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, fmt.Errorf("ping postgres: %w", err)
	}
	return &Store{Pool: pool}, nil
}

// Migrate applies the current schema (idempotent).
func (s *Store) Migrate(ctx context.Context) error {
	if _, err := s.Pool.Exec(ctx, schema); err != nil {
		return fmt.Errorf("apply schema: %w", err)
	}
	return nil
}

// Close releases the connection pool.
func (s *Store) Close() { s.Pool.Close() }

// InTx runs fn inside a database transaction, committing on success and
// rolling back on error. If fn panics, the transaction is rolled back and
// the panic is re-raised so crash simulations behave like a hard abort
// (no partial commit, no swallowed panic).
func (s *Store) InTx(ctx context.Context, fn func(pgx.Tx) error) (retErr error) {
	// READ COMMITTED is sufficient and safe here: every state-changing
	// operation first takes a keyed PostgreSQL advisory lock (chain- or
	// channel-scoped), and idempotency/uniqueness are additionally backed
	// by conditional unique indexes. SERIALIZABLE caused spurious 40001
	// serialization failures under the rapid header/message submission
	// pattern without adding any real guarantee on top of those locks.
	tx, err := s.Pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.ReadCommitted})
	if err != nil {
		return err
	}
	committed := false
	defer func() {
		if p := recover(); p != nil {
			_ = tx.Rollback(ctx)
			panic(p)
		}
		if !committed {
			_ = tx.Rollback(ctx)
		}
	}()
	if err := fn(tx); err != nil {
		return err
	}
	if err := tx.Commit(ctx); err != nil {
		return err
	}
	committed = true
	return nil
}
