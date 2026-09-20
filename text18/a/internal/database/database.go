package database

import (
	"context"

	"github.com/jackc/pgx/v5/pgxpool"
)

// RunMigrations acquires a single connection and applies every pending
// embedded migration.
func RunMigrations(ctx context.Context, pool *pgxpool.Pool) error {
	conn, err := pool.Acquire(ctx)
	if err != nil {
		return err
	}
	c := conn.Hijack()
	defer c.Close(ctx)
	return Up(ctx, c)
}

// ResetForTests tears down and re-applies the whole schema. It is intended
// for the integration test suite and local development only.
func ResetForTests(ctx context.Context, pool *pgxpool.Pool) error {
	conn, err := pool.Acquire(ctx)
	if err != nil {
		return err
	}
	c := conn.Hijack()
	defer c.Close(ctx)
	if err := Reset(ctx, c); err != nil {
		return err
	}
	return Up(ctx, c)
}
