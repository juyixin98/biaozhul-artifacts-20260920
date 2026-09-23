// Package integration contains end-to-end tests that require PostgreSQL.
//
// They use COSTLENS_TEST_DATABASE_URL (default points at the local docker
// Postgres). Each test gets its own throwaway database, migrated from scratch.
package integration

import (
	"context"
	"fmt"
	"net/url"
	"os"
	"strings"
	"testing"
	"time"

	"costlens/internal/migrate"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// Local default matches `docker compose up` (host port 55432, see
// docker-compose.yml); override with COSTLENS_TEST_DATABASE_URL.
const localURL = "postgres://costlens:costlens@localhost:55432/postgres?sslmode=disable"

func baseURL() string {
	if v := strings.TrimSpace(os.Getenv("COSTLENS_TEST_DATABASE_URL")); v != "" {
		return v
	}
	return localURL
}

type testDB struct {
	pool   *pgxpool.Pool
	dbName string
	url    string
}

// newTestDB creates a uniquely named scratch database and migrates it.
func newTestDB(t *testing.T) *testDB {
	t.Helper()
	root := baseURL()
	ctx := context.Background()

	admin, err := pgx.Connect(ctx, root)
	if err != nil {
		t.Skipf("postgres not reachable (%v); skipping integration test", err)
	}
	name := fmt.Sprintf("costlens_test_%d", time.Now().UnixNano())
	if _, err := admin.Exec(ctx, `DROP DATABASE IF EXISTS `+name); err != nil {
		t.Fatalf("drop db: %v", err)
	}
	if _, err := admin.Exec(ctx, `CREATE DATABASE `+name); err != nil {
		t.Fatalf("create db: %v", err)
	}
	_ = admin.Close(ctx)

	scratch := withDB(root, name)
	pool, err := pgxpool.New(ctx, scratch)
	if err != nil {
		t.Fatal(err)
	}
	if err := applySchema(ctx, pool); err != nil {
		t.Fatal(err)
	}

	t.Cleanup(func() {
		pool.Close()
		c, err := pgx.Connect(ctx, root)
		if err == nil {
			_, _ = c.Exec(ctx, `DROP DATABASE IF EXISTS `+name)
			_ = c.Close(ctx)
		}
	})
	return &testDB{pool: pool, dbName: name, url: scratch}
}

func withDB(root, db string) string {
	u, err := url.Parse(root)
	if err != nil {
		return root
	}
	u.Path = "/" + db
	return u.String()
}

func applySchema(ctx context.Context, pool *pgxpool.Pool) error {
	conn, err := pool.Acquire(ctx)
	if err != nil {
		return err
	}
	defer conn.Release()
	tx, err := conn.Begin(ctx)
	if err != nil {
		return err
	}
	if err := migrate.Up(ctx, tx); err != nil {
		_ = tx.Rollback(ctx)
		return err
	}
	return tx.Commit(ctx)
}
