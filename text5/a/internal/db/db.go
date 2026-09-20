package db

import (
	"context"
	"fmt"
	"io/fs"
	"sort"
	"strings"
	"time"

	"github.com/jmoiron/sqlx"
	_ "github.com/lib/pq"
)

// Connect opens the database, retrying briefly so the app can start in
// docker-compose while Postgres is still coming up.
func Connect(ctx context.Context, url string) (*sqlx.DB, error) {
	var db *sqlx.DB
	var err error
	for attempt := 0; attempt < 30; attempt++ {
		db, err = sqlx.ConnectContext(ctx, "postgres", url)
		if err == nil {
			return db, nil
		}
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(time.Second):
		}
	}
	return nil, fmt.Errorf("connect to database: %w", err)
}

// Migrate applies embedded *.sql migrations in filename order, each in its
// own transaction, recording them in schema_migrations.
func Migrate(ctx context.Context, db *sqlx.DB, migrations fs.FS) error {
	var names []string
	err := fs.WalkDir(migrations, ".", func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if !d.IsDir() && strings.HasSuffix(path, ".sql") {
			names = append(names, path)
		}
		return nil
	})
	if err != nil {
		return err
	}
	sort.Strings(names)

	// The migrations table itself is created by 0001_init.sql; before that we
	// cannot record anything, so probe for its existence first.
	var hasTable bool
	if err := db.GetContext(ctx, &hasTable, `
		SELECT EXISTS (
			SELECT 1 FROM information_schema.tables
			WHERE table_schema = 'public' AND table_name = 'schema_migrations'
		)`); err != nil {
		return err
	}

	applied := map[int]bool{}
	if hasTable {
		var versions []int
		if err := db.SelectContext(ctx, &versions, `SELECT version FROM schema_migrations`); err != nil {
			return err
		}
		for _, v := range versions {
			applied[v] = true
		}
	}

	for i, name := range names {
		version := i + 1
		if applied[version] {
			continue
		}
		body, err := fs.ReadFile(migrations, name)
		if err != nil {
			return err
		}
		tx, err := db.BeginTxx(ctx, nil)
		if err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, string(body)); err != nil {
			tx.Rollback()
			return fmt.Errorf("migration %s: %w", name, err)
		}
		if _, err := tx.ExecContext(ctx,
			`INSERT INTO schema_migrations (version) VALUES ($1) ON CONFLICT DO NOTHING`, version); err != nil {
			tx.Rollback()
			return fmt.Errorf("migration %s: record version: %w", name, err)
		}
		if err := tx.Commit(); err != nil {
			return err
		}
	}
	return nil
}
