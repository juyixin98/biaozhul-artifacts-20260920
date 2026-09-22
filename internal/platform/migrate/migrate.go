// Package migrate applies the embedded SQL migrations exactly once per file,
// tracking applied files in schema_migrations. Each file runs in its own
// transaction so a failure leaves a clean state.
package migrate

import (
	"context"
	"embed"
	"errors"
	"fmt"
	"io/fs"
	"sort"
	"strings"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

//go:embed all:sqlfiles
var migrationsFS embed.FS

const sqlDir = "sqlfiles"

func Up(ctx context.Context, pool *pgxpool.Pool) (int, error) {
	if _, err := pool.Exec(ctx, `
		CREATE TABLE IF NOT EXISTS schema_migrations (
			filename TEXT PRIMARY KEY,
			applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
		)`); err != nil {
		return 0, err
	}

	entries, err := fs.ReadDir(migrationsFS, "sqlfiles")
	if err != nil {
		return 0, err
	}
	names := make([]string, 0, len(entries))
	for _, e := range entries {
		if !e.IsDir() && strings.HasSuffix(e.Name(), ".sql") {
			names = append(names, e.Name())
		}
	}
	sort.Strings(names)

	applied := 0
	for _, name := range names {
		var exists bool
		if err := pool.QueryRow(ctx,
			`SELECT EXISTS(SELECT 1 FROM schema_migrations WHERE filename = $1)`, name,
		).Scan(&exists); err != nil {
			return applied, err
		}
		if exists {
			continue
		}
		sqlBytes, err := migrationsFS.ReadFile("sqlfiles/" + name)
		if err != nil {
			return applied, err
		}
		tx, err := pool.Begin(ctx)
		if err != nil {
			return applied, err
		}
		if _, err := tx.Exec(ctx, string(sqlBytes)); err != nil {
			_ = tx.Rollback(ctx)
			return applied, fmt.Errorf("migration %s: %w", name, err)
		}
		if _, err := tx.Exec(ctx,
			`INSERT INTO schema_migrations(filename) VALUES ($1)`, name); err != nil {
			_ = tx.Rollback(ctx)
			return applied, err
		}
		if err := tx.Commit(ctx); err != nil {
			return applied, err
		}
		applied++
	}
	return applied, nil
}

// Reset drops every DAMS table — test harness only.
func Reset(ctx context.Context, pool *pgxpool.Pool) error {
	return pgx.BeginFunc(ctx, pool, func(tx pgx.Tx) error {
		rows, err := tx.Query(ctx, `
			SELECT tablename FROM pg_tables
			WHERE schemaname = 'public' AND tablename <> 'schema_migrations'`)
		if err != nil {
			return err
		}
		var tables []string
		for rows.Next() {
			var t string
			if err := rows.Scan(&t); err != nil {
				rows.Close()
				return err
			}
			tables = append(tables, t)
		}
		rows.Close()
		if err := rows.Err(); err != nil {
			return err
		}
		if len(tables) == 0 {
			return nil
		}
		if _, err := tx.Exec(ctx, "DROP TABLE IF EXISTS "+strings.Join(tables, ",")+" CASCADE"); err != nil {
			return errors.Join(err)
		}
		return nil
	})
}
