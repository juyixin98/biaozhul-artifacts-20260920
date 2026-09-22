// Package dbtest provides real-PostgreSQL test databases. Each call to NewDB
// creates an isolated schema (via search_path) with migrations applied, so
// tests may run in parallel without interfering.
//
// The database URL comes from TEST_DATABASE_URL, falling back to DATABASE_URL
// and then the local default used by docker-compose. scripts/run-tests.sh
// starts a throwaway Postgres container and exports TEST_DATABASE_URL.
package dbtest

import (
	"context"
	"fmt"
	"os"
	"sync/atomic"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"communitygov/internal/database"
)

var schemaCounter int64

func dbURL(t testing.TB) string {
	t.Helper()
	for _, key := range []string{"TEST_DATABASE_URL", "DATABASE_URL"} {
		if v := os.Getenv(key); v != "" {
			return v
		}
	}
	return "postgres://gov:gov@localhost:5432/gov?sslmode=disable"
}

// NewDB returns a migrated store backed by a unique schema. Tests are skipped
// (not failed) when no database is reachable, so `go test ./...` stays usable
// in environments without Postgres; scripts/run-tests.sh always provides one.
func NewDB(t testing.TB) *database.Store {
	t.Helper()
	ctx := context.Background()

	base, err := pgxpool.ParseConfig(dbURL(t))
	if err != nil {
		t.Fatalf("parse db url: %v", err)
	}

	admin, err := pgxpool.NewWithConfig(ctx, base)
	if err != nil {
		t.Skipf("postgres not available (%v); run scripts/run-tests.sh", err)
	}
	defer admin.Close()
	if err := admin.Ping(ctx); err != nil {
		t.Skipf("postgres not reachable (%v); run scripts/run-tests.sh", err)
	}

	n := atomic.AddInt64(&schemaCounter, 1)
	schema := fmt.Sprintf("t_%d_%d", time.Now().UnixNano(), n)
	if _, err := admin.Exec(ctx, "CREATE SCHEMA "+schema); err != nil {
		t.Fatalf("create schema: %v", err)
	}
	t.Cleanup(func() {
		c := context.Background()
		_, _ = admin.Exec(c, "DROP SCHEMA IF EXISTS "+schema+" CASCADE")
		admin.Close()
	})

	cfg := base.Copy()
	if cfg.ConnConfig.RuntimeParams == nil {
		cfg.ConnConfig.RuntimeParams = map[string]string{}
	}
	cfg.ConnConfig.RuntimeParams["search_path"] = schema
	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		t.Fatalf("open test pool: %v", err)
	}
	t.Cleanup(pool.Close)

	if err := database.Migrate(ctx, pool); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	return database.NewStore(pool)
}
