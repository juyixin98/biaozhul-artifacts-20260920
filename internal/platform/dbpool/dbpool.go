// Package dbpool wires the pgx connection pool and the transaction helper.
package dbpool

import (
	"context"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

func New(ctx context.Context, url string) (*pgxpool.Pool, error) {
	cfg, err := pgxpool.ParseConfig(url)
	if err != nil {
		return nil, err
	}
	cfg.MaxConns = 10
	return pgxpool.NewWithConfig(ctx, cfg)
}

// InTx runs fn inside one transaction; a returned error rolls it back,
// nil commits. This is the guarantee the batch-ingest endpoint relies on:
// any error anywhere in the batch rolls back every row.
func InTx(ctx context.Context, pool *pgxpool.Pool, fn func(pgx.Tx) error) error {
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
