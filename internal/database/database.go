package database

import (
	"embed"
	"fmt"
	"sort"
	"strings"

	_ "github.com/jackc/pgx/v5/stdlib"
	"github.com/jmoiron/sqlx"
)

//go:embed migrations/*.sql
var migrationFS embed.FS

// Connect opens the database and verifies the connection.
func Connect(url string) (*sqlx.DB, error) {
	db, err := sqlx.Open("pgx", url)
	if err != nil {
		return nil, err
	}
	if err := db.Ping(); err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(20)
	return db, nil
}

// Migrate runs every not-yet-applied .sql migration in lexical order inside
// one transaction per file. Applied filenames are tracked in schema_migrations.
func Migrate(db *sqlx.DB) error {
	entries, err := migrationFS.ReadDir("migrations")
	if err != nil {
		return err
	}
	names := make([]string, 0, len(entries))
	for _, e := range entries {
		if !e.IsDir() && strings.HasSuffix(e.Name(), ".sql") {
			names = append(names, e.Name())
		}
	}
	sort.Strings(names)

	if _, err := db.Exec(`CREATE TABLE IF NOT EXISTS schema_migrations (
		name TEXT PRIMARY KEY,
		applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
	)`); err != nil {
		return err
	}

	for _, name := range names {
		var exists bool
		if err := db.Get(&exists, `SELECT EXISTS(SELECT 1 FROM schema_migrations WHERE name = $1)`, name); err != nil {
			return err
		}
		if exists {
			continue
		}
		content, err := migrationFS.ReadFile("migrations/" + name)
		if err != nil {
			return err
		}
		tx, err := db.Begin()
		if err != nil {
			return err
		}
		if _, err := tx.Exec(string(content)); err != nil {
			_ = tx.Rollback()
			return fmt.Errorf("migration %s: %w", name, err)
		}
		if _, err := tx.Exec(`INSERT INTO schema_migrations(name) VALUES ($1)`, name); err != nil {
			_ = tx.Rollback()
			return err
		}
		if err := tx.Commit(); err != nil {
			return err
		}
	}
	return nil
}
