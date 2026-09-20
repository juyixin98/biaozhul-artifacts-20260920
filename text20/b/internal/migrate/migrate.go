// Package migrate runs the embedded SQL migrations. Migrations are numbered
// pairs (NNNN_name.up.sql / .down.sql); the applied set is tracked in
// schema_migrations. Forward-only on startup; each migration runs in its own
// transaction (DDL is transactional in PostgreSQL).
package migrate

import (
	"context"
	"embed"
	"errors"
	"fmt"
	"sort"
	"strings"

	"github.com/jackc/pgx/v5"
)

//go:embed sql/*.sql
var migrationFS embed.FS

func Up(ctx context.Context, conn *pgx.Conn) error {
	if _, err := conn.Exec(ctx, `
		CREATE TABLE IF NOT EXISTS schema_migrations (
			version TEXT PRIMARY KEY,
			applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
		)`); err != nil {
		return fmt.Errorf("create schema_migrations: %w", err)
	}

	entries, err := migrationFS.ReadDir("sql")
	if err != nil {
		return err
	}
	ups := map[string]string{}
	var versions []string
	for _, e := range entries {
		name := e.Name()
		if strings.HasSuffix(name, ".up.sql") {
			v := strings.TrimSuffix(name, ".up.sql")
			b, err := migrationFS.ReadFile("sql/" + name)
			if err != nil {
				return err
			}
			ups[v] = string(b)
			versions = append(versions, v)
		}
	}
	sort.Strings(versions)

	for _, v := range versions {
		var exists bool
		if err := conn.QueryRow(ctx,
			`SELECT EXISTS(SELECT 1 FROM schema_migrations WHERE version=$1)`, v).Scan(&exists); err != nil {
			return err
		}
		if exists {
			continue
		}
		tx, err := conn.Begin(ctx)
		if err != nil {
			return err
		}
		if _, err := tx.Exec(ctx, ups[v]); err != nil {
			_ = tx.Rollback(ctx)
			return fmt.Errorf("migration %s: %w", v, err)
		}
		if _, err := tx.Exec(ctx,
			`INSERT INTO schema_migrations(version) VALUES($1)`, v); err != nil {
			_ = tx.Rollback(ctx)
			return err
		}
		if err := tx.Commit(ctx); err != nil {
			return err
		}
	}
	return nil
}

// Reset drops everything known to schema_migrations' opposite files and
// re-applies. Intended for tests/demo only.
func Reset(ctx context.Context, conn *pgx.Conn) error {
	entries, err := migrationFS.ReadDir("sql")
	if err != nil {
		return err
	}
	var downs []string
	for _, e := range entries {
		name := e.Name()
		if strings.HasSuffix(name, ".down.sql") {
			b, err := migrationFS.ReadFile("sql/" + name)
			if err != nil {
				return err
			}
			downs = append(downs, string(b))
		}
	}
	sort.Sort(sort.Reverse(sort.StringSlice(downs)))
	for _, d := range downs {
		if _, err := conn.Exec(ctx, d); err != nil {
			return err
		}
	}
	if _, err := conn.Exec(ctx, `DROP TABLE IF EXISTS schema_migrations`); err != nil {
		return err
	}
	return Up(ctx, conn)
}

var errStop = errors.New("stop")

// ensure pgx is referenced even if only pgxpool is used elsewhere
var _ = pgx.ErrNoRows
