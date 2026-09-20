// Package testsupport provisions a freshly migrated, empty PostgreSQL
// database for integration tests. It is intended only from _test.go files.
//
// Set TEST_DATABASE_ADMIN_URL to a DSN that can CREATE/DROP DATABASE.
// Defaults to the local postgres superuser over TCP with sslmode=disable.
package testsupport

import (
	"context"
	"fmt"
	"net/url"
	"os"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"signalboard/internal/migrate"
	"signalboard/internal/service"
)

const DefaultAdminURL = "postgres://postgres@localhost:5432/postgres?sslmode=disable"

type Env struct {
	Pool   *pgxpool.Pool
	Svc    *service.Service
	DBName string
	admin  *pgx.Conn
}

var (
	probeOnce sync.Once
	probeOK   bool
)

// New creates a unique migrated database for t and registers cleanup.
// Tests are skipped (not failed) when no PostgreSQL server is reachable.
func New(t *testing.T) *Env {
	t.Helper()
	ctx := context.Background()

	adminURL := os.Getenv("TEST_DATABASE_ADMIN_URL")
	if adminURL == "" {
		adminURL = DefaultAdminURL
	}

	probeOnce.Do(func() {
		c, err := pgx.Connect(ctx, adminURL)
		if err == nil {
			probeOK = true
			_ = c.Close(ctx)
		}
	})
	if !probeOK {
		t.Skipf("integration test needs PostgreSQL (%s); skipping", adminURL)
	}

	admin, err := pgx.Connect(ctx, adminURL)
	if err != nil {
		t.Fatalf("admin connect: %v", err)
	}

	// Unique across package runs (the counter alone resets each run).
	name := fmt.Sprintf("sb_test_%d_%d", time.Now().UnixNano(), seq.Add(1))
	if _, err := admin.Exec(ctx,
		`SELECT pg_terminate_backend(pid) FROM pg_stat_activity
		 WHERE datname = $1 AND pid <> pg_backend_pid()`, name); err != nil {
		_ = admin.Close(ctx)
		t.Fatalf("terminate connections to %s: %v", name, err)
	}
	if _, err := admin.Exec(ctx,
		fmt.Sprintf(`DROP DATABASE IF EXISTS %q`, name)); err != nil {
		_ = admin.Close(ctx)
		t.Fatalf("drop database %s: %v", name, err)
	}
	if _, err := admin.Exec(ctx,
		fmt.Sprintf(`CREATE DATABASE %q`, name)); err != nil {
		_ = admin.Close(ctx)
		t.Fatalf("create database %s: %v", name, err)
	}

	pool, err := pgxpool.New(ctx, withDB(adminURL, name))
	if err != nil {
		t.Fatalf("pool: %v", err)
	}
	conn, err := pool.Acquire(ctx)
	if err != nil {
		t.Fatalf("acquire: %v", err)
	}
	if err := migrate.Up(ctx, conn.Conn()); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	conn.Release()

	env := &Env{Pool: pool, Svc: service.New(pool), DBName: name, admin: admin}
	t.Cleanup(env.close)
	return env
}

var seq atomic.Int64

func (e *Env) close() {
	ctx := context.Background()
	e.Pool.Close()
	_, _ = e.admin.Exec(ctx,
		`SELECT pg_terminate_backend(pid) FROM pg_stat_activity
		 WHERE datname = $1 AND pid <> pg_backend_pid()`, e.DBName)
	_, _ = e.admin.Exec(ctx, fmt.Sprintf(`DROP DATABASE IF EXISTS %q`, e.DBName))
	_ = e.admin.Close(ctx)
}

func withDB(adminURL, dbName string) string {
	u, err := url.Parse(adminURL)
	if err != nil {
		return adminURL
	}
	u.Path = "/" + dbName
	return u.String()
}

// SeedStore creates a store plus n dishes and returns their IDs.
func (e *Env) SeedStore(t *testing.T, n int, tz string) (int64, []int64) {
	t.Helper()
	ctx := context.Background()
	st, err := e.Svc.CreateStore(ctx, "test store", tz)
	if err != nil {
		t.Fatalf("create store: %v", err)
	}
	ids := make([]int64, 0, n)
	for i := 0; i < n; i++ {
		d, err := e.Svc.CreateDish(ctx, st.ID, fmt.Sprintf("dish-%d", i), int64(100+i*10))
		if err != nil {
			t.Fatalf("create dish: %v", err)
		}
		ids = append(ids, d.ID)
	}
	return st.ID, ids
}

// DraftAndPublish replaces the draft with all dishes and publishes.
func (e *Env) DraftAndPublish(t *testing.T, storeID int64, dishIDs []int64, expected int64) service.MenuVersion {
	t.Helper()
	ctx := context.Background()
	items := make([]service.DraftItemInput, len(dishIDs))
	for i, id := range dishIDs {
		items[i] = service.DraftItemInput{DishID: id}
	}
	if _, err := e.Svc.ReplaceDraft(ctx, storeID, items); err != nil {
		t.Fatalf("draft: %v", err)
	}
	v, err := e.Svc.Publish(ctx, storeID, expected, "test", nil)
	if err != nil {
		t.Fatalf("publish: %v", err)
	}
	return v
}
