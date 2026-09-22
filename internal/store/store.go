// Package store wires PostgreSQL access: connection, embedded migrations and
// transaction helpers shared by all services.
package store

import (
	"context"
	"database/sql"
	"embed"
	"errors"
	"fmt"
	"sort"
	"time"

	"github.com/jackc/pgx/v5/pgconn"
	_ "github.com/jackc/pgx/v5/stdlib" // register the "pgx" database/sql driver
	"github.com/jmoiron/sqlx"
)

//go:embed migrations/*.sql
var migrationFS embed.FS

// ErrNotFound is the canonical no-row error.
var ErrNotFound = errors.New("store: not found")

// IsNotFound maps sql.ErrNoRows.
func IsNotFound(err error) bool { return errors.Is(err, ErrNotFound) }

// IsUniqueViolation reports a unique-constraint violation by SQLSTATE 23505.
func IsUniqueViolation(err error) bool {
	_, ok := UniqueConstraint(err)
	return ok
}

// UniqueConstraint returns the violated constraint name when applicable.
func UniqueConstraint(err error) (string, bool) {
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) && pgErr.Code == "23505" {
		return pgErr.ConstraintName, true
	}
	return "", false
}

// IsSerialFailure reports SQLSTATE 40001 (serialization failure) or 40P01
// (deadlock detected), both of which are safe to retry transparently.
func IsSerialFailure(err error) bool {
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) {
		return pgErr.Code == "40001" || pgErr.Code == "40P01"
	}
	return false
}

// Open connects using the pgx stdlib driver (sqlx manages the pool).
func Open(ctx context.Context, databaseURL string) (*sqlx.DB, error) {
	db, err := sqlx.ConnectContext(ctx, "pgx", databaseURL)
	if err != nil {
		return nil, fmt.Errorf("store: connect: %w", err)
	}
	db.SetMaxOpenConns(25)
	db.SetMaxIdleConns(5)
	return db, nil
}

// EnsureOpen waits for the database to accept connections (Docker Compose may
// start the app before Postgres is ready) and then returns the pool.
func EnsureOpen(ctx context.Context, databaseURL string) (*sqlx.DB, error) {
	var lastErr error
	for attempt := 0; attempt < 60; attempt++ {
		db, err := Open(ctx, databaseURL)
		if err == nil {
			if pingErr := db.PingContext(ctx); pingErr == nil {
				return db, nil
			} else {
				lastErr = pingErr
				_ = db.Close()
			}
		} else {
			lastErr = err
		}
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(time.Second):
		}
	}
	return nil, fmt.Errorf("store: database unreachable: %w", lastErr)
}

// RunMigrations applies every embedded migration not yet recorded. Safe to
// invoke on every startup, which gives restart-and-resume schema behavior.
func RunMigrations(ctx context.Context, db *sqlx.DB) error {
	_, err := db.ExecContext(ctx, `
		CREATE TABLE IF NOT EXISTS schema_migrations (
			name TEXT PRIMARY KEY,
			applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
		)`)
	if err != nil {
		return fmt.Errorf("store: migrations table: %w", err)
	}
	const dir = "migrations"
	entries, err := migrationFS.ReadDir(dir)
	if err != nil {
		return fmt.Errorf("store: read migrations: %w", err)
	}
	names := make([]string, 0, len(entries))
	for _, e := range entries {
		if !e.IsDir() && len(e.Name()) > 4 && e.Name()[len(e.Name())-4:] == ".sql" {
			names = append(names, e.Name())
		}
	}
	sort.Strings(names)
	for _, name := range names {
		var exists bool
		if err := db.GetContext(ctx, &exists,
			`SELECT EXISTS(SELECT 1 FROM schema_migrations WHERE name=$1)`, name); err != nil {
			return err
		}
		if exists {
			continue
		}
		sqlBytes, err := migrationFS.ReadFile(dir + "/" + name)
		if err != nil {
			return err
		}
		if err := InTx(ctx, db, func(tx *sqlx.Tx) error {
			if _, err := tx.ExecContext(ctx, string(sqlBytes)); err != nil {
				return fmt.Errorf("migration %s: %w", name, err)
			}
			if _, err := tx.ExecContext(ctx,
				`INSERT INTO schema_migrations(name) VALUES ($1)`, name); err != nil {
				return err
			}
			return nil
		}); err != nil {
			return err
		}
	}
	return nil
}

// InTx runs fn inside a SERIALIZABLE transaction, retrying on serialization
// failure or deadlock. SERIALIZABLE is what makes concurrent
// register/renew/transfer/sweep attempts unable to interleave into double
// ownership or double charges.
func InTx(ctx context.Context, db *sqlx.DB, fn func(tx *sqlx.Tx) error) error {
	const maxRetries = 10
	var err error
	for i := 0; i < maxRetries; i++ {
		var tx *sqlx.Tx
		tx, err = db.BeginTxx(ctx, &sql.TxOptions{Isolation: sql.LevelSerializable})
		if err != nil {
			return err
		}
		err = fn(tx)
		if err == nil {
			if err = tx.Commit(); err == nil {
				return nil
			}
		} else {
			_ = tx.Rollback()
		}
		if !IsSerialFailure(err) {
			return err
		}
	}
	return fmt.Errorf("store: serializable retry budget exhausted: %w", err)
}
