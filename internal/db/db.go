// Package db handles PostgreSQL connections and embedded SQL migrations.
package db

import (
	"context"
	"embed"
	"fmt"
	"net/url"
	"sort"
	"strings"

	_ "github.com/jackc/pgx/v5/stdlib"
	"github.com/jmoiron/sqlx"
)

//go:embed migrations/*.sql
var migrationFS embed.FS

//go:embed seed.sql
var seedSQL string

// Connect opens the connection pool and verifies it with a ping. Every pooled
// session runs in UTC so timestamptz values scan back as unambiguous UTC
// instants; employee-local time is always derived in application code.
func Connect(ctx context.Context, dsn string) (*sqlx.DB, error) {
	dsn = withUTCOption(dsn)
	d, err := sqlx.ConnectContext(ctx, "pgx", dsn)
	if err != nil {
		return nil, fmt.Errorf("connect database: %w", err)
	}
	return d, nil
}

// withUTCOption sets the timezone GUC for every new session via a DSN runtime
// parameter (pgx establishes the session with it applied).
func withUTCOption(dsn string) string {
	if strings.Contains(dsn, "timezone=") {
		return dsn
	}
	if strings.HasPrefix(dsn, "postgres://") || strings.HasPrefix(dsn, "postgresql://") {
		u, err := url.Parse(dsn)
		if err != nil {
			return dsn
		}
		q := u.Query()
		q.Set("timezone", "UTC")
		u.RawQuery = q.Encode()
		return u.String()
	}
	// Keyword/value DSN (space separated key=value pairs).
	return "timezone=UTC " + dsn
}

// Migrate applies every up-migration not yet recorded in schema_migrations.
// Migrations are embedded into the binary; each file is a single version and
// versions run once in lexicographic file order, inside their own transaction.
func Migrate(ctx context.Context, d *sqlx.DB) error {
	if _, err := d.ExecContext(ctx, `
		create table if not exists schema_migrations (
			version     text primary key,
			applied_at  timestamptz not null default now()
		)`); err != nil {
		return fmt.Errorf("create schema_migrations: %w", err)
	}

	entries, err := migrationFS.ReadDir("migrations")
	if err != nil {
		return err
	}
	var files []string
	for _, e := range entries {
		if strings.HasSuffix(e.Name(), ".up.sql") {
			files = append(files, e.Name())
		}
	}
	sort.Strings(files)

	for _, f := range files {
		var exists bool
		if err := d.GetContext(ctx, &exists,
			`select exists(select 1 from schema_migrations where version = $1)`, f); err != nil {
			return err
		}
		if exists {
			continue
		}
		sqlBytes, err := migrationFS.ReadFile("migrations/" + f)
		if err != nil {
			return err
		}
		tx, err := d.BeginTxx(ctx, nil)
		if err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, string(sqlBytes)); err != nil {
			_ = tx.Rollback()
			return fmt.Errorf("apply migration %s: %w", f, err)
		}
		if _, err := tx.ExecContext(ctx,
			`insert into schema_migrations(version) values ($1)`, f); err != nil {
			_ = tx.Rollback()
			return err
		}
		if err := tx.Commit(); err != nil {
			return err
		}
	}
	return nil
}

// Seed inserts reference data (departments, employees, sample keys, initial
// policy/classification). It is idempotent and never overwrites published data.
func Seed(ctx context.Context, d *sqlx.DB) error {
	_, err := d.ExecContext(ctx, seedSQL)
	if err != nil {
		return fmt.Errorf("seed: %w", err)
	}
	return nil
}
