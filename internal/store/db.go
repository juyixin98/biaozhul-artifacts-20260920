package store

import (
	"context"
	"database/sql"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"gorm.io/driver/mysql"
	"gorm.io/gorm"
	"gorm.io/gorm/schema"
)

// Open connects to MySQL, retrying until the database is reachable (useful in
// docker-compose where MySQL starts slightly slower than the app).
func Open(dsn string) (*gorm.DB, error) {
	db, err := sql.Open("mysql", dsn)
	if err != nil {
		return nil, err
	}
	var lastErr error
	for attempt := 0; attempt < 30; attempt++ {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		if err := db.PingContext(ctx); err != nil {
			lastErr = err
			cancel()
			time.Sleep(time.Second)
			continue
		}
		cancel()
		break
	}
	if lastErr != nil {
		// final ping check
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		if err := db.PingContext(ctx); err != nil {
			return nil, fmt.Errorf("mysql not reachable: %w", lastErr)
		}
	}
	db.SetMaxOpenConns(20)
	db.SetMaxIdleConns(10)
	db.SetConnMaxLifetime(time.Hour)

	gdb, err := gorm.Open(mysql.New(mysql.Config{
		Conn: db,
	}), &gorm.Config{
		NamingStrategy: schema.NamingStrategy{},
		// We manage the schema explicitly with versioned SQL files.
		DisableForeignKeyConstraintWhenMigrating: true,
		SkipDefaultTransaction:                   true,
	})
	if err != nil {
		return nil, err
	}
	return gdb, nil
}

// Migrate runs every not-yet-applied *.sql file in dir in lexical order and
// records them in schema_migrations. Each file is applied as one
// multi-statement exec (DSN must include multiStatements=true).
func Migrate(gdb *gorm.DB, dir string) error {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return fmt.Errorf("read migration dir: %w", err)
	}
	var files []string
	for _, e := range entries {
		if !e.IsDir() && strings.HasSuffix(e.Name(), ".sql") {
			files = append(files, e.Name())
		}
	}
	sort.Strings(files)

	// The bookkeeping table is created by the runner, not by a numbered
	// migration, so the very first run can already track applied files.
	if err := gdb.Exec(`CREATE TABLE IF NOT EXISTS schema_migrations (
		version VARCHAR(255) NOT NULL,
		applied_at DATETIME(6) NOT NULL,
		PRIMARY KEY (version)
	) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4`).Error; err != nil {
		return err
	}

	for _, name := range files {
		var exists int
		if err := gdb.Raw("SELECT COUNT(1) FROM schema_migrations WHERE version = ?", name).
			Scan(&exists).Error; err != nil {
			return err
		}
		if exists > 0 {
			continue
		}
		b, err := os.ReadFile(filepath.Join(dir, name))
		if err != nil {
			return err
		}
		if err := gdb.Exec(string(b)).Error; err != nil {
			return fmt.Errorf("apply migration %s: %w", name, err)
		}
		if err := gdb.Exec("INSERT INTO schema_migrations (version, applied_at) VALUES (?, UTC_TIMESTAMP(6))", name).Error; err != nil {
			return err
		}
	}
	return nil
}
