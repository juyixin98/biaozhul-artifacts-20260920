package db

import (
	"embed"
	"fmt"
	"log"
	"os"
	"sort"
	"strings"
	"time"

	"gorm.io/driver/mysql"
	"gorm.io/gorm"
	gormlogger "gorm.io/gorm/logger"
)

//go:embed migrations/*.sql
var migrationFS embed.FS

// Open establishes the GORM connection. The DSN is expected to carry
// multiStatements=true so embedded SQL files may run in one exec.
func Open(dsn string) (*gorm.DB, error) {
	gdb, err := gorm.Open(mysql.Open(dsn), &gorm.Config{
		// Record-not-found is an expected, handled control-flow signal;
		// silence it while still logging genuine SQL errors.
		Logger: gormlogger.New(
			log.New(os.Stdout, "\r\n", log.LstdFlags),
			gormlogger.Config{
				SlowThreshold:             200 * time.Millisecond,
				LogLevel:                  gormlogger.Error,
				IgnoreRecordNotFoundError: true,
				Colorful:                  true,
			},
		),
	})
	if err != nil {
		return nil, err
	}
	sqlDB, err := gdb.DB()
	if err != nil {
		return nil, err
	}
	sqlDB.SetMaxOpenConns(20)
	sqlDB.SetMaxIdleConns(5)
	sqlDB.SetConnMaxLifetime(time.Hour)
	return gdb, nil
}

// Migrate applies every embedded migration in lexical order, tracking applied
// versions in schema_migrations.
func Migrate(gdb *gorm.DB) error {
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

	return gdb.Connection(func(tx *gorm.DB) error {
		if err := tx.Exec(`CREATE TABLE IF NOT EXISTS schema_migrations (
			version VARCHAR(32) NOT NULL,
			applied_at DATETIME(3) NOT NULL,
			PRIMARY KEY (version)
		) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4`).Error; err != nil {
			return err
		}

		var applied []string
		if err := tx.Raw("SELECT version FROM schema_migrations").Scan(&applied).Error; err != nil {
			return err
		}
		done := make(map[string]struct{}, len(applied))
		for _, v := range applied {
			done[v] = struct{}{}
		}

		for _, name := range names {
			version := strings.TrimSuffix(name, ".sql")
			if _, ok := done[version]; ok {
				continue
			}
			sqlBytes, rerr := migrationFS.ReadFile("migrations/" + name)
			if rerr != nil {
				return rerr
			}
			if err := tx.Exec(string(sqlBytes)).Error; err != nil {
				return fmt.Errorf("migration %s: %w", name, err)
			}
			if err := tx.Exec("INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
				version, time.Now().UTC()).Error; err != nil {
				return err
			}
		}
		return nil
	})
}

// Ping verifies the database is reachable.
func Ping(gdb *gorm.DB) error {
	sqlDB, err := gdb.DB()
	if err != nil {
		return err
	}
	return sqlDB.Ping()
}
