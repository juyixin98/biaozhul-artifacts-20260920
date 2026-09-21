// Package db embeds SQL migrations and applies them in order inside a
// schema_migrations tracking table.
package db

import (
	"crypto/sha256"
	"embed"
	"encoding/hex"
	"fmt"
	"sort"

	"github.com/jmoiron/sqlx"
)

//go:embed migrations_sql/*.sql
var migrationFS embed.FS

// Migrate applies every embedded migration exactly once, inside one
// transaction per migration.
func Migrate(d *sqlx.DB) error {
	if _, err := d.Exec(`CREATE TABLE IF NOT EXISTS schema_migrations (
		version TEXT PRIMARY KEY,
		checksum TEXT NOT NULL,
		applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
	)`); err != nil {
		return fmt.Errorf("create migrations table: %w", err)
	}

	entries, err := migrationFS.ReadDir("migrations_sql")
	if err != nil {
		return err
	}
	names := make([]string, 0, len(entries))
	for _, e := range entries {
		if !e.IsDir() {
			names = append(names, e.Name())
		}
	}
	sort.Strings(names)

	for _, name := range names {
		sqlBytes, err := migrationFS.ReadFile("migrations_sql/" + name)
		if err != nil {
			return err
		}
		checksum := sha256.Sum256(sqlBytes)
		cs := hex.EncodeToString(checksum[:])

		var existing string
		err = d.Get(&existing, `SELECT checksum FROM schema_migrations WHERE version = $1`, name)
		if err == nil {
			if existing != cs {
				return fmt.Errorf("migration %s checksum mismatch", name)
			}
			continue
		}

		tx, err := d.Beginx()
		if err != nil {
			return err
		}
		if _, err := tx.Exec(string(sqlBytes)); err != nil {
			_ = tx.Rollback()
			return fmt.Errorf("apply migration %s: %w", name, err)
		}
		if _, err := tx.Exec(`INSERT INTO schema_migrations(version, checksum) VALUES ($1, $2)`, name, cs); err != nil {
			_ = tx.Rollback()
			return err
		}
		if err := tx.Commit(); err != nil {
			return err
		}
	}
	return nil
}
