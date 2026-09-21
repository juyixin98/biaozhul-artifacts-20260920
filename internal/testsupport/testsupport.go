// Package testsupport provides a throwaway MySQL schema per test.
//
// Tests need a MySQL server; point TEST_MYSQL_DSN at it, e.g.
//
//	TEST_MYSQL_DSN='root:rootpass@tcp(127.0.0.1:13306)/?charset=utf8mb4&parseTime=true&loc=UTC'
//
// scripts/test-mysql.sh starts a disposable container on port 13306 with
// exactly these credentials. Tests are skipped when the DSN is unset.
package testsupport

import (
	"fmt"
	"os"
	"strings"
	"testing"
	"time"

	"gorm.io/driver/mysql"
	"gorm.io/gorm"
	gormlogger "gorm.io/gorm/logger"

	"targetcraft/internal/db"
)

// OpenTestDB creates a fresh database, migrates it, and drops it on cleanup.
func OpenTestDB(t *testing.T) *gorm.DB {
	t.Helper()
	dsn := testDSN()
	if dsn == "" {
		t.Skip("TEST_MYSQL_DSN not set; skipping MySQL-backed test")
	}

	admin, err := gorm.Open(mysql.Open(dsn), &gorm.Config{
		Logger: gormlogger.Default.LogMode(gormlogger.Silent),
	})
	if err != nil {
		t.Fatalf("connect test mysql: %v", err)
	}

	name := fmt.Sprintf("targetcraft_test_%d", time.Now().UnixNano())
	if err := admin.Exec("CREATE DATABASE " + name + " CHARACTER SET utf8mb4").Error; err != nil {
		t.Fatalf("create test database: %v", err)
	}
	t.Cleanup(func() {
		_ = admin.Exec("DROP DATABASE IF EXISTS " + name).Error
		sqlDB, _ := admin.DB()
		_ = sqlDB.Close()
	})

	gdb, err := gorm.Open(mysql.Open(withDatabase(dsn, name)), &gorm.Config{
		Logger: gormlogger.Default.LogMode(gormlogger.Silent),
	})
	if err != nil {
		t.Fatalf("connect test database: %v", err)
	}
	if err := db.Migrate(gdb); err != nil {
		t.Fatalf("migrate test database: %v", err)
	}
	t.Cleanup(func() {
		sqlDB, _ := gdb.DB()
		_ = sqlDB.Close()
	})
	return gdb
}

func testDSN() string {
	return os.Getenv("TEST_MYSQL_DSN")
}

// withDatabase rewrites the DSN's database segment (the part between the
// closing ")" of the address and the query string) to name.
func withDatabase(dsn, name string) string {
	i := strings.LastIndex(dsn, ")/")
	if i < 0 {
		return dsn
	}
	head := dsn[:i+2]
	tail := dsn[i+2:]
	if j := strings.Index(tail, "?"); j >= 0 {
		return head + name + tail[j:]
	}
	return head + name
}
