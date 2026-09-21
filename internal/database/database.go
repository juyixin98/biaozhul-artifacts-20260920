// Package database opens and migrates the GORM database.
package database

import (
	"fmt"
	"strings"

	"forensiccore/internal/domain"

	"github.com/glebarez/sqlite"
	"gorm.io/driver/mysql"
	"gorm.io/gorm"
	gormlogger "gorm.io/gorm/logger"
)

// Open connects using driver ("mysql" | "sqlite"), runs AutoMigrate and
// returns a configured *gorm.DB.
func Open(driver, dsn string, verbose bool) (*gorm.DB, error) {
	logLevel := gormlogger.Warn
	if verbose {
		logLevel = gormlogger.Info
	}
	cfg := &gorm.Config{Logger: gormlogger.Default.LogMode(logLevel)}

	var db *gorm.DB
	var err error
	switch driver {
	case "mysql":
		db, err = gorm.Open(mysql.Open(dsn), cfg)
	case "sqlite":
		// busy_timeout is a per-connection pragma, so it is attached to the
		// DSN and applied to every pooled connection. SQLite is supported for
		// local development and tests only; a single connection serializes
		// writes and avoids shared-cache table locks. Production uses MySQL.
		dsn = withSQLitePragmas(dsn)
		db, err = gorm.Open(sqlite.Open(dsn), cfg)
	default:
		return nil, fmt.Errorf("unknown driver %q", driver)
	}
	if err != nil {
		return nil, fmt.Errorf("open %s: %w", driver, err)
	}

	if driver == "sqlite" {
		sqlDB, err := db.DB()
		if err != nil {
			return nil, err
		}
		sqlDB.SetMaxOpenConns(1)
		// Enforce foreign keys on every connection.
		if err := db.Exec("PRAGMA foreign_keys = ON").Error; err != nil {
			return nil, err
		}
		// WAL only applies to file-backed databases; ignore failures on memory DSNs.
		_ = db.Exec("PRAGMA journal_mode = WAL").Error
	}

	if err := db.AutoMigrate(domain.AllModels()...); err != nil {
		return nil, fmt.Errorf("automigrate: %w", err)
	}
	return db, nil
}

// withSQLitePragmas appends busy_timeout/foreign_keys pragmas to a sqlite DSN
// unless the caller already set them.
func withSQLitePragmas(dsn string) string {
	add := func(dsn, pragma string) string {
		if strings.Contains(dsn, pragma) {
			return dsn
		}
		sep := "?"
		if strings.Contains(dsn, "?") {
			sep = "&"
		}
		return dsn + sep + "_pragma=" + pragma
	}
	dsn = add(dsn, "busy_timeout(5000)")
	return dsn
}
