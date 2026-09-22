// Package migrate is a deliberately small SQL-file migration runner:
// files in the migrations directory are applied in lexical order, each once,
// and recorded in schema_migrations. Multi-statement files are executed with
// the simple protocol (no prepared statements), which is what allows DDL
// constructs (and CTE-based seed SQL) to run unchanged.
package migrate

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/jackc/pgx/v5"
)

type Runner struct {
	conn       *pgx.Conn
	migrations string
}

func New(conn *pgx.Conn, migrationsDir string) *Runner {
	return &Runner{conn: conn, migrations: migrationsDir}
}

func (r *Runner) ensureTable(ctx context.Context) error {
	_, err := r.conn.Exec(ctx, `
		CREATE TABLE IF NOT EXISTS schema_migrations (
			version TEXT PRIMARY KEY,
			applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
		)`)
	return err
}

// Migrate applies every not-yet-recorded .sql file in lexical order.
func (r *Runner) Migrate(ctx context.Context) error {
	if err := r.ensureTable(ctx); err != nil {
		return err
	}
	entries, err := os.ReadDir(r.migrations)
	if err != nil {
		return fmt.Errorf("read migrations dir: %w", err)
	}
	var files []string
	for _, e := range entries {
		if !e.IsDir() && strings.HasSuffix(e.Name(), ".sql") {
			files = append(files, e.Name())
		}
	}
	sort.Strings(files)

	applied := map[string]bool{}
	rows, err := r.conn.Query(ctx, "SELECT version FROM schema_migrations")
	if err != nil {
		return err
	}
	for rows.Next() {
		var v string
		if err := rows.Scan(&v); err != nil {
			rows.Close()
			return err
		}
		applied[v] = true
	}
	rows.Close()

	for _, f := range files {
		if applied[f] {
			continue
		}
		path := filepath.Join(r.migrations, f)
		sqlBytes, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		if _, err := r.conn.Exec(ctx, string(sqlBytes)); err != nil {
			return fmt.Errorf("apply %s: %w", f, err)
		}
		if _, err := r.conn.Exec(ctx,
			"INSERT INTO schema_migrations (version) VALUES ($1)", f); err != nil {
			return err
		}
	}
	return nil
}

// Seed executes a seed file when the users table is empty; it is idempotent.
func Seed(ctx context.Context, conn *pgx.Conn, seedFile string) error {
	var n int
	if err := conn.QueryRow(ctx, "SELECT count(*) FROM users").Scan(&n); err != nil {
		return err
	}
	if n > 0 {
		return nil
	}
	sqlBytes, err := os.ReadFile(seedFile)
	if err != nil {
		return err
	}
	_, err = conn.Exec(ctx, string(sqlBytes))
	return err
}
