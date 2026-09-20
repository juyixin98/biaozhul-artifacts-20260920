// Migrations are embedded from this package's migrations/ directory and
// applied by Up/RunMigrations. The runner is intentionally dependency-free:
// each migration is tracked in schema_migrations.
package database

import (
	"context"
	"embed"
	"fmt"
	"io/fs"
	"sort"
	"strings"

	"github.com/jackc/pgx/v5"
)

//go:embed migrations/*.sql
var migrationFS embed.FS

func ensureTable(ctx context.Context, conn *pgx.Conn) error {
	_, err := conn.Exec(ctx, `
		CREATE TABLE IF NOT EXISTS schema_migrations (
			version    integer PRIMARY KEY,
			name       text NOT NULL,
			applied_at timestamptz NOT NULL DEFAULT now()
		)`)
	return err
}

// Up applies every pending *.up.sql migration. Each migration runs in its
// own transaction, so a failure leaves no half-applied migration.
func Up(ctx context.Context, pool *pgx.Conn) error {
	if err := ensureTable(ctx, pool); err != nil {
		return err
	}
	entries, err := fs.ReadDir(migrationFS, "migrations")
	if err != nil {
		return err
	}
	var names []string
	for _, e := range entries {
		if !e.IsDir() && strings.HasSuffix(e.Name(), ".up.sql") {
			names = append(names, e.Name())
		}
	}
	sort.Strings(names)

	tx, err := pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)

	for _, n := range names {
		var version int
		if _, err := fmt.Sscanf(n, "%d_", &version); err != nil {
			return fmt.Errorf("parse migration version %q: %w", n, err)
		}
		var exists bool
		if err := tx.QueryRow(ctx,
			`SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE version = $1)`,
			version).Scan(&exists); err != nil {
			return err
		}
		if exists {
			continue
		}
		sqlBytes, err := migrationFS.ReadFile("migrations/" + n)
		if err != nil {
			return err
		}
		if _, err := tx.Exec(ctx, string(sqlBytes)); err != nil {
			return fmt.Errorf("migration %s: %w", n, err)
		}
		if _, err := tx.Exec(ctx,
			`INSERT INTO schema_migrations (version, name) VALUES ($1, $2)`,
			version, n); err != nil {
			return err
		}
	}
	return tx.Commit(ctx)
}

// Reset drops every schema_migrations-tracked object by executing the
// down scripts in reverse version order. Intended for local tests only.
func Reset(ctx context.Context, pool *pgx.Conn) error {
	if err := ensureTable(ctx, pool); err != nil {
		return err
	}
	entries, err := fs.ReadDir(migrationFS, "migrations")
	if err != nil {
		return err
	}
	downs := map[string]string{}
	for _, e := range entries {
		if strings.HasSuffix(e.Name(), ".down.sql") {
			b, err := migrationFS.ReadFile("migrations/" + e.Name())
			if err != nil {
				return err
			}
			downs[e.Name()] = string(b)
		}
	}
	var downNames []string
	for n := range downs {
		downNames = append(downNames, n)
	}
	sort.Sort(sort.Reverse(sort.StringSlice(downNames)))

	tx, err := pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)

	for _, n := range downNames {
		if _, err := tx.Exec(ctx, downs[n]); err != nil {
			return fmt.Errorf("down migration %s: %w", n, err)
		}
		var version int
		if _, err := fmt.Sscanf(n, "%d_", &version); err != nil {
			return err
		}
		_, _ = tx.Exec(ctx, `DELETE FROM schema_migrations WHERE version = $1`, version)
	}
	return tx.Commit(ctx)
}
