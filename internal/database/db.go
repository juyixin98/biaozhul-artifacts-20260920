package database

import (
	"context"
	"embed"
	"fmt"
	"sort"
	"strings"

	"github.com/jackc/pgx/v5/pgxpool"

	"communitygov/internal/database/sqlcgen"
)

//go:embed migrations_sql/*.sql
var migrationFS embed.FS

// Store wraps sqlc queries and the pool. InTx runs fn inside a transaction.
// all concurrency-sensitive service logic goes through it so row locks
// (SELECT ... FOR UPDATE) and conditional updates are atomic.
type Store struct {
	pool *pgxpool.Pool
	*sqlcgen.Queries
}

func NewStore(pool *pgxpool.Pool) *Store {
	return &Store{pool: pool, Queries: sqlcgen.New(pool)}
}

func (s *Store) Pool() *pgxpool.Pool { return s.pool }

type ctxKey struct{}

// InTx runs fn inside a transaction. Nested InTx calls with the propagated
// context join the SAME transaction (no savepoints needed — any error rolls
// the whole unit back, which is what moderation + reporting compound actions
// rely on). All concurrency-sensitive logic goes through here so row locks
// (SELECT ... FOR UPDATE) and conditional updates are atomic together.
func (s *Store) InTx(ctx context.Context, fn func(ctx context.Context, q *sqlcgen.Queries) error) error {
	if q, ok := ctx.Value(ctxKey{}).(*sqlcgen.Queries); ok {
		return fn(ctx, q)
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback(ctx) }()

	q := sqlcgen.New(tx)
	txCtx := context.WithValue(ctx, ctxKey{}, q)
	if err := fn(txCtx, q); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

// txQueries extracts the tx-bound queries when already inside InTx.
func txQueries(ctx context.Context) (*sqlcgen.Queries, bool) {
	q, ok := ctx.Value(ctxKey{}).(*sqlcgen.Queries)
	return q, ok
}

// Migrate applies all embedded up migrations in lexical order. Each file is one
// version; already applied versions are tracked in schema_migrations.
func Migrate(ctx context.Context, pool *pgxpool.Pool) error {
	entries, err := migrationFS.ReadDir("migrations_sql")
	if err != nil {
		return err
	}
	var files []string
	for _, e := range entries {
		if !e.IsDir() && strings.HasSuffix(e.Name(), ".up.sql") {
			files = append(files, e.Name())
		}
	}
	sort.Strings(files)

	if _, err := pool.Exec(ctx, `
		CREATE TABLE IF NOT EXISTS schema_migrations (
			version TEXT PRIMARY KEY,
			applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
		)`); err != nil {
		return err
	}

	for _, f := range files {
		var exists bool
		if err := pool.QueryRow(ctx,
			`SELECT EXISTS(SELECT 1 FROM schema_migrations WHERE version=$1)`, f).Scan(&exists); err != nil {
			return err
		}
		if exists {
			continue
		}
		sqlBytes, err := migrationFS.ReadFile("migrations_sql/" + f)
		if err != nil {
			return err
		}
		tx, err := pool.Begin(ctx)
		if err != nil {
			return err
		}
		if _, err := tx.Exec(ctx, string(sqlBytes)); err != nil {
			_ = tx.Rollback(ctx)
			return fmt.Errorf("apply %s: %w", f, err)
		}
		if _, err := tx.Exec(ctx,
			`INSERT INTO schema_migrations(version) VALUES ($1)`, f); err != nil {
			_ = tx.Rollback(ctx)
			return err
		}
		if err := tx.Commit(ctx); err != nil {
			return err
		}
	}
	return nil
}
