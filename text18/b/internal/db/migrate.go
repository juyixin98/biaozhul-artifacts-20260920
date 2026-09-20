package db

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/jackc/pgx/v5"
)

// Migrate applies every *.up.sql file in dir in lexical order exactly once,
// recording applied versions in schema_migrations. It is an embedded-migration
// free runner so the only dependency to operate is PostgreSQL itself.
func Migrate(ctx context.Context, conn *pgx.Conn, dir string) error {
	if _, err := conn.Exec(ctx, `
		CREATE TABLE IF NOT EXISTS schema_migrations (
			version    TEXT PRIMARY KEY,
			applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
		)`); err != nil {
		return fmt.Errorf("create schema_migrations: %w", err)
	}

	entries, err := os.ReadDir(dir)
	if err != nil {
		return fmt.Errorf("read migrations dir %q: %w", dir, err)
	}
	var ups []string
	for _, e := range entries {
		if !e.IsDir() && strings.HasSuffix(e.Name(), ".up.sql") {
			ups = append(ups, e.Name())
		}
	}
	sort.Strings(ups)

	for _, name := range ups {
		var exists bool
		if err := conn.QueryRow(ctx,
			`SELECT EXISTS(SELECT 1 FROM schema_migrations WHERE version = $1)`, name,
		).Scan(&exists); err != nil {
			return err
		}
		if exists {
			continue
		}
		sqlBytes, err := os.ReadFile(filepath.Join(dir, name))
		if err != nil {
			return err
		}
		tx, err := conn.Begin(ctx)
		if err != nil {
			return err
		}
		if _, err := tx.Exec(ctx, string(sqlBytes)); err != nil {
			_ = tx.Rollback(ctx)
			return fmt.Errorf("apply %s: %w", name, err)
		}
		if _, err := tx.Exec(ctx,
			`INSERT INTO schema_migrations (version) VALUES ($1)`, name); err != nil {
			_ = tx.Rollback(ctx)
			return err
		}
		if err := tx.Commit(ctx); err != nil {
			return err
		}
	}
	return nil
}

// MigrateDown rolls back every *.down.sql in reverse order. Only used by tests
// and the reset make target; never wired into normal startup.
func MigrateDown(ctx context.Context, conn *pgx.Conn, dir string) error {
	entries, err := os.ReadDir(dir)
	if err == nil {
		var downs []string
		for _, e := range entries {
			if !e.IsDir() && strings.HasSuffix(e.Name(), ".down.sql") {
				downs = append(downs, e.Name())
			}
		}
		sort.Sort(sort.Reverse(sort.StringSlice(downs)))
		for _, name := range downs {
			sqlBytes, err := os.ReadFile(filepath.Join(dir, name))
			if err != nil {
				return err
			}
			if _, err := conn.Exec(ctx, string(sqlBytes)); err != nil {
				return fmt.Errorf("revert %s: %w", name, err)
			}
			upName := strings.Replace(name, ".down.sql", ".up.sql", 1)
			_, _ = conn.Exec(ctx, `DELETE FROM schema_migrations WHERE version = $1`, upName)
		}
	}
	_, err = conn.Exec(ctx, `DROP TABLE IF EXISTS schema_migrations`)
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return err
	}
	return nil
}
