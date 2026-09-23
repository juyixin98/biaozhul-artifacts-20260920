// Package migrate applies embedded SQL migrations transactionally.
package migrate

import (
	"context"
	_ "embed"
	"errors"
	"fmt"
	"sort"
	"strings"

	"github.com/jackc/pgx/v5"
)

//go:embed 0001_init.sql
var initSQL string

var migrations = []struct {
	version string
	sql     string
}{
	{"0001_init", initSQL},
}

// Up runs every migration not yet recorded in schema_migrations. Each migration
// and its bookkeeping insert happen in one transaction: partial application is
// impossible.
func Up(ctx context.Context, tx pgx.Tx) error {
	if _, err := tx.Exec(ctx, `
		CREATE TABLE IF NOT EXISTS schema_migrations (
			version text PRIMARY KEY,
			applied_at timestamptz NOT NULL DEFAULT now()
		)`); err != nil {
		return err
	}

	applied := map[string]bool{}
	rows, err := tx.Query(ctx, "SELECT version FROM schema_migrations")
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
	if err := rows.Err(); err != nil {
		return err
	}

	pending := make([]struct {
		version string
		sql     string
	}, 0, len(migrations))
	for _, m := range migrations {
		if !applied[m.version] {
			pending = append(pending, m)
		}
	}
	sort.Slice(pending, func(i, j int) bool { return pending[i].version < pending[j].version })

	for _, m := range pending {
		if _, err := tx.Exec(ctx, m.sql); err != nil {
			return fmt.Errorf("migration %s: %w", m.version, err)
		}
		if _, err := tx.Exec(ctx,
			"INSERT INTO schema_migrations(version) VALUES ($1)", m.version); err != nil {
			return err
		}
	}
	return nil
}

// Ensure applies migrations using the connection/pool directly, wrapping the
// whole batch in a single transaction.
func Ensure(ctx context.Context, db pgx.Tx) error {
	if db == nil {
		return errors.New("nil tx")
	}
	return Up(ctx, db)
}

// SQLFile returns the init script, used by Docker entrypoint and tests.
func SQLFile() string { return strings.TrimSpace(initSQL) }
