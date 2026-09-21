// Package migrate runs embedded versioned SQL migrations.
package migrate

import (
	"embed"
	"fmt"
	"sort"
	"time"

	"gorm.io/gorm"
)

//go:embed *.sql
var migrationsFS embed.FS

// Up applies every embedded migration not yet recorded in schema_migrations.
func Up(db *gorm.DB) ([]string, error) {
	if err := db.Exec(`CREATE TABLE IF NOT EXISTS schema_migrations (
		version VARCHAR(64) NOT NULL PRIMARY KEY,
		applied_at DATETIME(3) NOT NULL
	) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4`).Error; err != nil {
		return nil, fmt.Errorf("ensure schema_migrations: %w", err)
	}
	var applied []string
	if err := db.Raw("SELECT version FROM schema_migrations").Scan(&applied).Error; err != nil {
		return nil, err
	}
	done := map[string]bool{}
	for _, v := range applied {
		done[v] = true
	}
	entries, err := migrationsFS.ReadDir(".")
	if err != nil {
		return nil, err
	}
	var names []string
	for _, e := range entries {
		if len(e.Name()) > 4 && e.Name()[len(e.Name())-4:] == ".sql" {
			names = append(names, e.Name())
		}
	}
	sort.Strings(names)

	var ran []string
	for _, name := range names {
		if done[name] {
			continue
		}
		sqlBytes, err := migrationsFS.ReadFile(name)
		if err != nil {
			return ran, err
		}
		err = db.Transaction(func(tx *gorm.DB) error {
			if err := tx.Exec(string(sqlBytes)).Error; err != nil {
				return fmt.Errorf("apply %s: %w", name, err)
			}
			return tx.Exec("INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
				name, time.Now().UTC()).Error
		})
		if err != nil {
			return ran, err
		}
		ran = append(ran, name)
	}
	return ran, nil
}
