// Package integration contains tests that exercise the REAL PostgreSQL
// store and the full HTTP stack. They are skipped unless TEST_DATABASE_URL
// points at a PostgreSQL database (the test TRUNCATES that database).
package integration

import (
	"context"
	"os"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"vci/internal/store"
)

func testURL() string {
	if v := os.Getenv("TEST_DATABASE_URL"); v != "" {
		return v
	}
	return "postgres://vc_test:vc_test@localhost:5432/vc_index_test?sslmode=disable"
}

// newTestStore connects, truncates all data and resets the snapshot
// sequence so every test starts from snapshot 0.
func newTestStore(t *testing.T) *store.PGStore {
	t.Helper()
	if os.Getenv("RUN_DB_TESTS") == "" {
		t.Skip("set RUN_DB_TESTS=1 (and TEST_DATABASE_URL if needed) to run PostgreSQL tests")
	}
	url := testURL()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	// Verify connectivity first so a missing DB yields a skip, not a crash.
	pool, err := pgxpool.New(ctx, url)
	if err != nil {
		t.Skipf("cannot connect to %s: %v", url, err)
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		t.Skipf("cannot ping %s: %v", url, err)
	}
	pool.Close()

	st, err := store.New(context.Background(), url)
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	t.Cleanup(st.Close)
	if err := st.Migrate(context.Background()); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	resetDB(t, url)
	return st
}

func resetDB(t *testing.T, url string) {
	t.Helper()
	ctx := context.Background()
	pool, err := pgxpool.New(ctx, url)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	stmts := []string{
		`TRUNCATE revocations, credentials, issuer_keys, issuers RESTART IDENTITY CASCADE`,
		// Reset sequence to the "never used" state: last_value NULL makes
		// the service's COALESCE(last_value, 0) report head 0.
		`SELECT setval('vc_snapshot_seq', 1, false)`,
	}
	for _, q := range stmts {
		if _, err := pool.Exec(ctx, q); err != nil {
			t.Fatalf("reset (%s): %v", q, err)
		}
	}
}

// poolFromStore opens a fresh pool to the same database for raw SQL
// assertions in tests.
func poolFromStore(t *testing.T, _ *store.PGStore) *pgxpool.Pool {
	t.Helper()
	pool, err := pgxpool.New(context.Background(), testURL())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)
	return pool
}
