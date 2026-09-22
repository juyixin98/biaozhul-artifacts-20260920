// Package migrate applies embedded SQL migrations in filename order.
package migrate

import (
	"context"
	"embed"
	"fmt"
	"sort"

	"github.com/jmoiron/sqlx"
)

//go:embed migrations/*.sql
var files embed.FS

// Up applies all pending migrations inside per-file transactions.
func Up(ctx context.Context, db *sqlx.DB) error {
	if _, err := db.ExecContext(ctx, `
		CREATE TABLE IF NOT EXISTS schema_migrations (
			version text PRIMARY KEY,
			applied_at timestamptz NOT NULL DEFAULT now()
		)`); err != nil {
		return err
	}
	names, err := files.ReadDir("migrations")
	if err != nil {
		return err
	}
	var versions []string
	for _, n := range names {
		versions = append(versions, n.Name())
	}
	sort.Strings(versions)
	for _, v := range versions {
		var applied bool
		if err := db.GetContext(ctx, &applied,
			`SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE version = $1)`, v); err != nil {
			return err
		}
		if applied {
			continue
		}
		body, err := files.ReadFile("migrations/" + v)
		if err != nil {
			return err
		}
		tx, err := db.BeginTxx(ctx, nil)
		if err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, string(body)); err != nil {
			tx.Rollback()
			return fmt.Errorf("migration %s: %w", v, err)
		}
		if _, err := tx.ExecContext(ctx,
			`INSERT INTO schema_migrations (version) VALUES ($1)`, v); err != nil {
			tx.Rollback()
			return err
		}
		if err := tx.Commit(); err != nil {
			return err
		}
	}
	return nil
}
