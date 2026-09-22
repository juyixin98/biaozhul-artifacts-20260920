// Package testdb provisions an isolated MySQL database for integration tests.
// It is only used by _test.go files.
//
// The server DSN is taken from TEST_MYSQL_DSN (without a database), e.g.
//
//	root:rootlocal@tcp(127.0.0.1:13323)/
//
// Each call creates a uniquely named database, applies migrations and seeds
// defaults. When no MySQL is reachable the test is skipped, unless
// TEST_REQUIRE_MYSQL=1 is set (then it fails).
package testdb

import (
	"database/sql"
	"fmt"
	"hash/fnv"
	"os"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"anomalywatch/internal/config"
	"anomalywatch/internal/database"
	"anomalywatch/internal/models"
	"anomalywatch/internal/seed"

	_ "github.com/go-sql-driver/mysql"
	"gorm.io/driver/mysql"
	"gorm.io/gorm"
	gormlogger "gorm.io/gorm/logger"
)

var counter uint32

// Env returns the server-level (no schema) DSN and whether tests may run.
func Env() (string, bool) {
	dsn := strings.TrimSpace(os.Getenv("TEST_MYSQL_DSN"))
	return dsn, dsn != ""
}

// New provisions a fresh database and returns a GORM handle plus a config
// pointing at it. Cleanup drops the database.
func New(t testing.TB) (*gorm.DB, config.Config) {
	t.Helper()
	serverDSN, ok := Env()
	if !ok {
		if os.Getenv("TEST_REQUIRE_MYSQL") == "1" {
			t.Fatal("TEST_MYSQL_DSN is required with TEST_REQUIRE_MYSQL=1")
		}
		t.Skip("TEST_MYSQL_DSN not set; skipping MySQL integration test")
	}

	cfg := config.Load()
	applyDSN(t, &cfg, serverDSN)

	name := uniqueName(t)
	createDB(t, serverDSN, name)
	cfg.DBName = name

	t.Cleanup(func() {
		dropDB(t, serverDSN, name)
	})

	gdb := connect(t, cfg)
	migrate(t, cfg)
	if err := seed.Run(gdb, cfg); err != nil {
		t.Fatalf("seed: %v", err)
	}
	return gdb, cfg
}

func uniqueName(t testing.TB) string {
	h := fnv.New32a()
	h.Write([]byte(t.Name()))
	h.Write([]byte(time.Now().Format("150405.000000")))
	n := atomic.AddUint32(&counter, 1)
	return fmt.Sprintf("aw_test_%d_%d", h.Sum32()%100000, n)
}

func createDB(t testing.TB, serverDSN, name string) {
	t.Helper()
	db, err := sql.Open("mysql", serverDSN)
	if err != nil {
		t.Fatalf("open server connection: %v", err)
	}
	defer db.Close()
	if _, err := db.Exec(fmt.Sprintf(
		"CREATE DATABASE `%s` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci", name)); err != nil {
		t.Fatalf("create database %s: %v", name, err)
	}
}

func dropDB(t testing.TB, serverDSN, name string) {
	t.Helper()
	db, err := sql.Open("mysql", serverDSN)
	if err != nil {
		return
	}
	defer db.Close()
	_, _ = db.Exec(fmt.Sprintf("DROP DATABASE IF EXISTS `%s`", name))
}

func connect(t testing.TB, cfg config.Config) *gorm.DB {
	t.Helper()
	var gdb *gorm.DB
	var err error
	for attempt := 0; attempt < 30; attempt++ {
		gdb, err = gorm.Open(mysql.Open(cfg.DSN(false)), &gorm.Config{
			Logger: gormlogger.Default.LogMode(gormlogger.Silent),
		})
		if err == nil {
			if sqlDB, derr := gdb.DB(); derr == nil {
				if perr := sqlDB.Ping(); perr == nil {
					return gdb
				}
			}
		}
		time.Sleep(500 * time.Millisecond)
	}
	t.Fatalf("connect test database: %v", err)
	return nil
}

func migrate(t testing.TB, cfg config.Config) {
	t.Helper()
	dir := migrationsDir()
	db, err := sql.Open("mysql", cfg.DSN(true))
	if err != nil {
		t.Fatalf("open migration connection: %v", err)
	}
	defer db.Close()
	if err := database.Migrate(db, dir); err != nil {
		t.Fatalf("migrate: %v", err)
	}
}

// applyDSN parses a MySQL DSN (user:pass@tcp(host:port)/db?params) and copies
// its connection coordinates into cfg.
func applyDSN(t testing.TB, cfg *config.Config, dsn string) {
	t.Helper()
	at := strings.Index(dsn, "@tcp(")
	if at < 0 {
		t.Fatalf("TEST_MYSQL_DSN must be a go-sql-driver DSN with @tcp(host:port)")
	}
	cred := dsn[:at]
	rest := dsn[at+len("@tcp("):]
	closeP := strings.Index(rest, ")")
	if closeP < 0 {
		t.Fatalf("malformed TEST_MYSQL_DSN")
	}
	hostPort := rest[:closeP]
	parts := strings.SplitN(hostPort, ":", 2)
	cfg.DBHost = parts[0]
	if len(parts) == 2 {
		cfg.DBPort = parts[1]
	}
	if i := strings.Index(cred, ":"); i >= 0 {
		cfg.DBUser, cfg.DBPass = cred[:i], cred[i+1:]
	} else {
		cfg.DBUser = cred
	}
}

// migrationsDir finds the repository migrations directory from the package
// working directory (internal/testdb).
func migrationsDir() string {
	wd, _ := os.Getwd()
	for i := 0; i < 6; i++ {
		cand := wd + "/migrations"
		if st, err := os.Stat(cand); err == nil && st.IsDir() {
			return cand
		}
		if j := strings.LastIndex(wd, "/"); j > 0 {
			wd = wd[:j]
		} else {
			break
		}
	}
	return "migrations"
}

// EmployeeIDs returns the seeded employee IDs keyed by name.
func EmployeeIDs(t testing.TB, db *gorm.DB) map[string]uint64 {
	t.Helper()
	var emps []models.Employee
	if err := db.Order("id").Find(&emps).Error; err != nil {
		t.Fatalf("load employees: %v", err)
	}
	out := map[string]uint64{}
	for _, e := range emps {
		out[e.Name] = e.ID
	}
	return out
}
