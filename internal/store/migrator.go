package store

import (
	"database/sql"
	"embed"
	"fmt"
	"sort"
	"strings"
)

//go:embed migrations/*.sql
var migrationFS embed.FS

// Migrator applies embedded versioned SQL files in filename order, tracking
// applied versions in schema_migrations. Each file runs in its own
// transaction; a failure aborts startup so the operator sees it immediately.
type Migrator struct {
	db *sql.DB
}

func NewMigrator(db *sql.DB) *Migrator { return &Migrator{db: db} }

func (m *Migrator) Up() error {
	if _, err := m.db.Exec(`CREATE TABLE IF NOT EXISTS schema_migrations (
		version VARCHAR(64) NOT NULL,
		applied_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
		PRIMARY KEY (version)
	) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4`); err != nil {
		return fmt.Errorf("create schema_migrations: %w", err)
	}

	entries, err := migrationFS.ReadDir("migrations")
	if err != nil {
		return err
	}
	var files []string
	for _, e := range entries {
		if strings.HasSuffix(e.Name(), ".sql") {
			files = append(files, e.Name())
		}
	}
	sort.Strings(files)

	for _, f := range files {
		var applied string
		err := m.db.QueryRow(`SELECT version FROM schema_migrations WHERE version = ?`, f).Scan(&applied)
		if err == nil {
			continue
		}
		if err != sql.ErrNoRows {
			return err
		}

		content, err := migrationFS.ReadFile("migrations/" + f)
		if err != nil {
			return err
		}

		tx, err := m.db.Begin()
		if err != nil {
			return err
		}
		// Split on ';' would break on statements containing semicolons; the
		// migration set is written one full statement per file execution via
		// multi-statement support of the driver (multiStatements=true).
		if _, err := tx.Exec(string(content)); err != nil {
			tx.Rollback()
			return fmt.Errorf("apply migration %s: %w", f, err)
		}
		if _, err := tx.Exec(`INSERT INTO schema_migrations(version) VALUES (?)`, f); err != nil {
			tx.Rollback()
			return err
		}
		if err := tx.Commit(); err != nil {
			return err
		}
	}
	return nil
}
