// Package store opens the PostgreSQL connection and applies embedded SQL
// migrations, tracked durably in schema_migrations.
package store

import (
	"context"
	"embed"
	"fmt"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/jmoiron/sqlx"
	_ "github.com/lib/pq"
)

//go:embed migrations/*.sql
var migrations embed.FS

// Open connects to PostgreSQL, retrying while the database server comes up
// (the compose healthcheck is usually enough, but local dev may lag).
func Open(ctx context.Context, dsn string, retries int) (*sqlx.DB, error) {
	var lastErr error
	for i := 0; i < retries; i++ {
		db, err := sqlx.ConnectContext(ctx, "postgres", dsn)
		if err == nil {
			return db, nil
		}
		lastErr = err
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(time.Second):
		}
	}
	return nil, fmt.Errorf("connecting to database: %w", lastErr)
}

// Migrate applies every embedded migration in filename order exactly once.
// Each migration runs in its own transaction together with the bookkeeping
// insert, so a failure leaves neither a half-applied schema nor a recorded
// version for a migration that did not commit.
func Migrate(ctx context.Context, db *sqlx.DB) error {
	if _, err := db.ExecContext(ctx, `
		CREATE TABLE IF NOT EXISTS schema_migrations (
			version    INTEGER PRIMARY KEY,
			applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
		)`); err != nil {
		return err
	}

	entries, err := migrations.ReadDir("migrations")
	if err != nil {
		return err
	}
	names := make([]string, 0, len(entries))
	for _, e := range entries {
		names = append(names, e.Name())
	}
	sort.Strings(names)

	for _, name := range names {
		version, err := strconv.Atoi(strings.SplitN(name, "_", 2)[0])
		if err != nil {
			return fmt.Errorf("bad migration filename %q: %w", name, err)
		}
		var applied bool
		if err := db.GetContext(ctx, &applied,
			`SELECT EXISTS(SELECT 1 FROM schema_migrations WHERE version=$1)`, version); err != nil {
			return err
		}
		if applied {
			continue
		}
		body, err := migrations.ReadFile("migrations/" + name)
		if err != nil {
			return err
		}
		tx, err := db.BeginTxx(ctx, nil)
		if err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, string(body)); err != nil {
			_ = tx.Rollback()
			return fmt.Errorf("migration %s: %w", name, err)
		}
		if _, err := tx.ExecContext(ctx,
			`INSERT INTO schema_migrations (version) VALUES ($1)`, version); err != nil {
			_ = tx.Rollback()
			return err
		}
		if err := tx.Commit(); err != nil {
			return err
		}
	}
	return nil
}
