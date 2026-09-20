// Package testutil provides shared test fixtures: a real MySQL 8 database
// (via testcontainers or TEST_MYSQL_DSN) and helpers for waiting on queue
// state. Queue/recovery tests run against InnoDB so SKIP LOCKED, row locks
// and unique indexes behave exactly like production.
//
// Each NewStore call gets its own freshly created schema, so tests never see
// each other's rows even when packages run as parallel test binaries. The
// DSN user therefore needs CREATE/DROP DATABASE privileges (the test
// containers connect as root; for TEST_MYSQL_DSN use a privileged test user).
package testutil

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"os"
	"regexp"
	"testing"
	"time"

	sqldriver "github.com/go-sql-driver/mysql"
	tc "github.com/testcontainers/testcontainers-go"
	tcwait "github.com/testcontainers/testcontainers-go/wait"
	gormmysql "gorm.io/driver/mysql"
	"gorm.io/gorm"
	gormlogger "gorm.io/gorm/logger"

	"sitevitals/internal/models"
	"sitevitals/internal/store"
)

// NewStore returns a migrated store backed by a throwaway schema. If
// TEST_MYSQL_DSN is set it is reused as the server connection; otherwise a
// MySQL 8.4 container is started for the test process.
func NewStore(t *testing.T) *store.Store {
	t.Helper()
	if testing.Short() {
		t.Skip("skipping MySQL-backed test in -short mode")
	}
	dsn := freshServerDSN(t)
	schema := uniqueSchemaName(t)
	bootstrap, err := gorm.Open(gormmysql.Open(withDatabase(dsn, "")), &gorm.Config{
		Logger: gormlogger.Default.LogMode(gormlogger.Silent),
	})
	if err != nil {
		t.Fatalf("bootstrap connect: %v", err)
	}
	if err := bootstrap.Exec("CREATE DATABASE `" + schema + "` CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci").Error; err != nil {
		t.Fatalf("create schema %s: %v", schema, err)
	}
	bootstrapSQL, _ := bootstrap.DB()
	_ = bootstrapSQL.Close()
	t.Cleanup(func() {
		b, err := gorm.Open(gormmysql.Open(withDatabase(dsn, "")), &gorm.Config{
			Logger: gormlogger.Default.LogMode(gormlogger.Silent),
		})
		if err == nil {
			_ = b.Exec("DROP DATABASE IF EXISTS `" + schema + "`").Error
			sql, _ := b.DB()
			_ = sql.Close()
		}
	})

	testDSN := withDatabase(dsn, schema)
	db, err := gorm.Open(gormmysql.Open(testDSN), &gorm.Config{
		Logger: gormlogger.Default.LogMode(gormlogger.Silent),
	})
	if err != nil {
		t.Fatalf("open db: %v", err)
	}
	st := store.New(db)
	ctx := context.Background()
	if err := st.AutoMigrate(ctx); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	if err := st.Seed(ctx); err != nil {
		t.Fatalf("seed: %v", err)
	}
	t.Cleanup(func() {
		sqlDB, _ := db.DB()
		_ = sqlDB.Close()
	})
	return st
}

var sharedDSN string

func freshServerDSN(t *testing.T) string {
	t.Helper()
	if dsn := os.Getenv("TEST_MYSQL_DSN"); dsn != "" {
		return dsn
	}
	if sharedDSN == "" {
		sharedDSN = startContainer(t)
	}
	return sharedDSN
}

func startContainer(t *testing.T) string {
	t.Helper()
	ctx := context.Background()
	// The cached image is mysql:8.4; launch generically and build the root
	// DSN ourselves (root is needed for per-test schema creation).
	req := tc.ContainerRequest{
		Image:        "mysql:8.4",
		ExposedPorts: []string{"3306/tcp"},
		Env: map[string]string{
			"MYSQL_ROOT_PASSWORD": "rootpw",
		},
		WaitingFor: tcwait.ForLog("ready for connections").WithOccurrence(2).
			WithStartupTimeout(90 * time.Second),
	}
	container, err := tc.GenericContainer(ctx, tc.GenericContainerRequest{
		ContainerRequest: req,
		Started:          true,
	})
	if err != nil {
		t.Fatalf("start mysql container (is Docker available?): %v", err)
	}
	t.Cleanup(func() {
		_ = container.Terminate(context.Background())
	})
	host, err := container.Host(ctx)
	if err != nil {
		t.Fatalf("container host: %v", err)
	}
	port, err := container.MappedPort(ctx, "3306/tcp")
	if err != nil {
		t.Fatalf("mapped port: %v", err)
	}
	dsn := "root:rootpw@tcp(" + host + ":" + port.Port() + ")/mysql?charset=utf8mb4&parseTime=true&loc=UTC&multiStatements=true"

	deadline := time.Now().Add(45 * time.Second)
	for time.Now().Before(deadline) {
		db, err := gorm.Open(gormmysql.Open(dsn), &gorm.Config{Logger: gormlogger.Default.LogMode(gormlogger.Silent)})
		if err == nil {
			sqlDB, _ := db.DB()
			if err = sqlDB.Ping(); err == nil {
				_ = sqlDB.Close()
				return dsn
			}
			_ = sqlDB.Close()
		}
		time.Sleep(500 * time.Millisecond)
	}
	t.Fatalf("mysql container never became ready")
	return ""
}

var dsnDBRe = regexp.MustCompile(`^(.*?@tcp\([^)]*\))/[^?]*(\?.*)?$`)

// withDatabase rewrites the schema segment of a go-sql-driver DSN.
func withDatabase(dsn, schema string) string {
	m := dsnDBRe.FindStringSubmatch(dsn)
	if m == nil {
		return dsn
	}
	return m[1] + "/" + schema + m[2]
}

func uniqueSchemaName(t *testing.T) string {
	t.Helper()
	b := make([]byte, 6)
	if _, err := rand.Read(b); err != nil {
		t.Fatal(err)
	}
	return "svtest_" + hex.EncodeToString(b)
}

// ensure the driver package is linked even if future edits drop an import.
var _ = sqldriver.ErrInvalidConn

// WaitForTaskStatus polls until a task reaches one of wantStates or times out.
func WaitForTaskStatus(t *testing.T, st *store.Store, taskID uint, wantStates ...string) *models.Task {
	t.Helper()
	deadline := time.Now().Add(30 * time.Second)
	for time.Now().Before(deadline) {
		task, err := st.GetTask(context.Background(), taskID)
		if err == nil {
			for _, w := range wantStates {
				if task.Status == w {
					return task
				}
			}
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatalf("task %d never reached %v", taskID, wantStates)
	return nil
}

// WaitFor polls fn until it returns true or the timeout elapses.
func WaitFor(t *testing.T, timeout time.Duration, fn func() bool) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if fn() {
			return
		}
		time.Sleep(25 * time.Millisecond)
	}
	t.Fatalf("condition not met within %s", timeout)
}
