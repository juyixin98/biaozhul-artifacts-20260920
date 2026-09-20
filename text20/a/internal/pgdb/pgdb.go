package pgdb

import (
	"context"
	"embed"
	"fmt"
	"sort"
	"strings"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

//go:embed migrations/*.sql
var migrationFS embed.FS

// Connect opens a pgx pool, waiting for PostgreSQL to accept connections.
func Connect(ctx context.Context, url string) (*pgxpool.Pool, error) {
	cfg, err := pgxpool.ParseConfig(url)
	if err != nil {
		return nil, fmt.Errorf("parse database url: %w", err)
	}
	cfg.MaxConns = 20
	cfg.MinConns = 1

	var pool *pgxpool.Pool
	deadline := time.Now().Add(60 * time.Second)
	for {
		pool, err = pgxpool.NewWithConfig(ctx, cfg)
		if err == nil {
			err = pool.Ping(ctx)
		}
		if err == nil {
			return pool, nil
		}
		if pool != nil {
			pool.Close()
		}
		if time.Now().After(deadline) {
			return nil, fmt.Errorf("connect to database: %w", err)
		}
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(time.Second):
		}
	}
}

// Migrate applies every embedded migration in filename order, tracking applied
// files in schema_migrations. Each migration runs in its own transaction.
func Migrate(ctx context.Context, pool *pgxpool.Pool) ([]string, error) {
	entries, err := migrationFS.ReadDir("migrations")
	if err != nil {
		return nil, err
	}
	var files []string
	for _, e := range entries {
		if !e.IsDir() && strings.HasSuffix(e.Name(), ".sql") {
			files = append(files, e.Name())
		}
	}
	sort.Strings(files)

	if _, err := pool.Exec(ctx, `
		CREATE TABLE IF NOT EXISTS schema_migrations (
			filename   TEXT PRIMARY KEY,
			applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
		)`); err != nil {
		return nil, fmt.Errorf("create schema_migrations: %w", err)
	}

	var applied []string
	for _, f := range files {
		var exists bool
		if err := pool.QueryRow(ctx,
			`SELECT EXISTS(SELECT 1 FROM schema_migrations WHERE filename = $1)`, f,
		).Scan(&exists); err != nil {
			return nil, err
		}
		if exists {
			continue
		}
		sqlBytes, err := migrationFS.ReadFile("migrations/" + f)
		if err != nil {
			return nil, err
		}
		tx, err := pool.Begin(ctx)
		if err != nil {
			return nil, err
		}
		if _, err := tx.Exec(ctx, string(sqlBytes)); err != nil {
			_ = tx.Rollback(ctx)
			return nil, fmt.Errorf("apply %s: %w", f, err)
		}
		if _, err := tx.Exec(ctx,
			`INSERT INTO schema_migrations (filename) VALUES ($1)`, f); err != nil {
			_ = tx.Rollback(ctx)
			return nil, err
		}
		if err := tx.Commit(ctx); err != nil {
			return nil, err
		}
		applied = append(applied, f)
	}
	return applied, nil
}
