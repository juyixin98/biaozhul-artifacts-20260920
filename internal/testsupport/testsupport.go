// Package testsupport provisions a disposable PostgreSQL database for the
// integration tests: it connects to communityvault_test, runs the real
// migrations and seed against it, and resets data between tests.
package testsupport

import (
	"context"
	"net/url"
	"os"
	"strings"
	"testing"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"communityvault/internal/migrate"
)

const adminDSN = "postgres://cv:cv@localhost:55440/communityvault_test?sslmode=disable"

// Pool returns a pool connected to the test database, or skips the test when
// no database is reachable (e.g. `go test` without docker compose up).
func Pool(t *testing.T) *pgxpool.Pool {
	t.Helper()
	dsn := os.Getenv("TEST_DATABASE_URL")
	if dsn == "" {
		dsn = adminDSN
	}
	ensureDatabase(t, dsn)

	pool, err := pgxpool.New(context.Background(), dsn)
	if err != nil {
		t.Skipf("connect test db: %v", err)
	}
	if err := pool.Ping(context.Background()); err != nil {
		pool.Close()
		t.Skipf("ping test db: %v (start with: docker compose up -d db)", err)
	}

	conn, err := pgx.Connect(context.Background(), dsn)
	if err != nil {
		t.Skipf("connect for migrations: %v", err)
	}
	if err := migrate.New(conn, "../../migrations").Migrate(context.Background()); err != nil {
		_ = conn.Close(context.Background())
		t.Skipf("migrate test db: %v", err)
	}
	if err := migrate.Seed(context.Background(), conn, "../../seed/seed.sql"); err != nil {
		_ = conn.Close(context.Background())
		t.Fatalf("seed test db: %v", err)
	}
	_ = conn.Close(context.Background())

	t.Cleanup(pool.Close)
	return pool
}

// Reset truncates every application table and re-seeds the demo users,
// categories and v1 rules.
func Reset(t *testing.T, pool *pgxpool.Pool) {
	t.Helper()
	ctx := context.Background()
	_, err := pool.Exec(ctx, `
		TRUNCATE review_claims, review_tasks, reviews, status_events, reports,
		         content_revisions, contents, sensitive_words, rule_versions,
		         moderator_categories, categories, users
		RESTART IDENTITY CASCADE`)
	if err != nil {
		t.Fatalf("truncate: %v", err)
	}
	dsn := os.Getenv("TEST_DATABASE_URL")
	if dsn == "" {
		dsn = adminDSN
	}
	conn, err := pgx.Connect(ctx, dsn)
	if err != nil {
		t.Fatalf("connect for reseed: %v", err)
	}
	defer conn.Close(ctx)
	if err := migrate.Seed(ctx, conn, "../../seed/seed.sql"); err != nil {
		t.Fatalf("reseed: %v", err)
	}
}

func ensureDatabase(t *testing.T, dsn string) {
	t.Helper()
	u, err := url.Parse(dsn)
	if err != nil {
		t.Skipf("parse dsn: %v", err)
	}
	dbName := u.Path[1:]
	u.Path = "/postgres"
	conn, err := pgx.Connect(context.Background(), u.String())
	if err != nil {
		t.Skipf("connect postgres db (run `docker compose up -d db`): %v", err)
	}
	defer conn.Close(context.Background())
	if _, err := conn.Exec(context.Background(), "CREATE DATABASE "+dbName); err != nil {
		if !strings.Contains(err.Error(), "already exists") {
			t.Logf("create db: %v", err)
		}
	}
}
