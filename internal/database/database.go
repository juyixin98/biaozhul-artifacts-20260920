package database

import (
	"database/sql"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"anomalywatch/internal/config"

	_ "github.com/go-sql-driver/mysql"
	"gorm.io/driver/mysql"
	"gorm.io/gorm"
	gormlogger "gorm.io/gorm/logger"
)

// Connect opens a GORM connection (and verifies it with a ping), retrying while
// MySQL is still starting up under docker compose.
func Connect(cfg config.Config) (*gorm.DB, error) {
	var gdb *gorm.DB
	var err error
	for attempt := 0; attempt < 30; attempt++ {
		gdb, err = gorm.Open(mysql.Open(cfg.DSN(false)), &gorm.Config{
			Logger: gormlogger.Default.LogMode(gormlogger.Warn),
		})
		if err == nil {
			var sqlDB *sql.DB
			if sqlDB, err = gdb.DB(); err == nil {
				sqlDB.SetMaxOpenConns(20)
				sqlDB.SetMaxIdleConns(10)
				sqlDB.SetConnMaxLifetime(time.Hour)
				err = sqlDB.Ping()
			}
		}
		if err == nil {
			return gdb, nil
		}
		time.Sleep(retryDelay(attempt))
	}
	return nil, fmt.Errorf("connect mysql after retries: %w", err)
}

// EnsureDatabase creates the schema if it does not exist.
func EnsureDatabase(cfg config.Config) error {
	rootDSN := fmt.Sprintf("%s:%s@tcp(%s:%s)/?charset=utf8mb4&parseTime=true&loc=UTC",
		cfg.DBUser, cfg.DBPass, cfg.DBHost, cfg.DBPort)
	var db *sql.DB
	var err error
	for attempt := 0; attempt < 30; attempt++ {
		db, err = sql.Open("mysql", rootDSN)
		if err == nil {
			err = db.Ping()
		}
		if err == nil {
			break
		}
		db.Close()
		time.Sleep(retryDelay(attempt))
	}
	if err != nil {
		return fmt.Errorf("connect server: %w", err)
	}
	defer db.Close()
	_, err = db.Exec(fmt.Sprintf(
		"CREATE DATABASE IF NOT EXISTS `%s` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci",
		cfg.DBName))
	return err
}

func retryDelay(attempt int) time.Duration {
	d := time.Duration(250*(attempt+1)) * time.Millisecond
	if d > 2*time.Second {
		d = 2 * time.Second
	}
	return d
}

// Migrate executes every *.sql file under dir in lexical order that has not
// been recorded in schema_migrations. The connection passed in must have
// multiStatements enabled.
func Migrate(db *sql.DB, dir string) error {
	if _, err := db.Exec(`CREATE TABLE IF NOT EXISTS schema_migrations (
		version VARCHAR(255) NOT NULL PRIMARY KEY,
		applied_at DATETIME(3) NOT NULL
	) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4`); err != nil {
		return err
	}

	entries, err := os.ReadDir(dir)
	if err != nil {
		return fmt.Errorf("read migrations dir: %w", err)
	}
	var files []string
	for _, e := range entries {
		if !e.IsDir() && strings.HasSuffix(e.Name(), ".sql") {
			files = append(files, e.Name())
		}
	}
	sort.Strings(files)

	for _, name := range files {
		var exists int
		if err := db.QueryRow(
			"SELECT COUNT(1) FROM schema_migrations WHERE version = ?", name,
		).Scan(&exists); err != nil {
			return err
		}
		if exists > 0 {
			continue
		}
		content, err := os.ReadFile(filepath.Join(dir, name))
		if err != nil {
			return err
		}
		tx, err := db.Begin()
		if err != nil {
			return err
		}
		if _, err := tx.Exec(string(content)); err != nil {
			_ = tx.Rollback()
			return fmt.Errorf("apply migration %s: %w", name, err)
		}
		if _, err := tx.Exec(
			"INSERT INTO schema_migrations(version, applied_at) VALUES (?, UTC_TIMESTAMP(3))", name,
		); err != nil {
			_ = tx.Rollback()
			return err
		}
		if err := tx.Commit(); err != nil {
			return err
		}
	}
	return nil
}
