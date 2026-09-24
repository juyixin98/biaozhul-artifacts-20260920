package testutil

import (
	"context"
	"fmt"
	"net/url"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// DSN returns the base test database DSN.
func DSN() string {
	if v := os.Getenv("TEST_DATABASE_URL"); v != "" {
		return v
	}
	return "postgres://deadlock:deadlock@localhost:55432/deadlock?sslmode=disable"
}

// IsolatedDSN creates (or recreates) a fresh database named after the test
// binary/package so packages running in parallel never TRUNCATE each other.
// The returned DSN points at the isolated database; cleanup drops it.
func IsolatedDSN(t *testing.T) string {
	t.Helper()
	base := DSN()
	dbName := "dl_test_" + sanitizeName(t.Name())

	admin, err := pgx.Connect(context.Background(), base)
	if err != nil {
		t.Skipf("postgres not available (%s): %v", base, err)
	}
	defer admin.Close(context.Background())

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := admin.Ping(ctx); err != nil {
		t.Skipf("postgres not reachable (%s): %v", base, err)
	}

	if _, err := admin.Exec(ctx, `DROP DATABASE IF EXISTS `+dbName+` WITH (FORCE)`); err != nil {
		t.Fatalf("drop isolated db: %v", err)
	}
	if _, err := admin.Exec(ctx, `CREATE DATABASE `+dbName); err != nil {
		t.Fatalf("create isolated db: %v", err)
	}
	t.Cleanup(func() {
		c, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		adm, err := pgx.Connect(c, base)
		if err == nil {
			_, _ = adm.Exec(c, `DROP DATABASE IF EXISTS `+dbName+` WITH (FORCE)`)
			adm.Close(c)
		}
	})

	return withDB(base, dbName)
}

// NewPool opens a pool to dsn and registers cleanup.
func NewPool(t *testing.T, dsn string) *pgxpool.Pool {
	t.Helper()
	pool, err := pgxpool.New(context.Background(), dsn)
	if err != nil {
		t.Fatalf("pool: %v", err)
	}
	t.Cleanup(pool.Close)
	return pool
}

func withDB(dsn, dbName string) string {
	u, err := url.Parse(dsn)
	if err != nil {
		// best effort string rewrite
		i := strings.Index(dsn, "/")
		return dsn[:i+1] + dbName
	}
	u.Path = "/" + dbName
	return u.String()
}

func sanitizeName(name string) string {
	name = strings.ToLower(name)
	var b strings.Builder
	for _, r := range name {
		switch {
		case r >= 'a' && r <= 'z', r >= '0' && r <= '9':
			b.WriteRune(r)
		default:
			b.WriteByte('_')
		}
	}
	out := b.String()
	if len(out) > 40 {
		out = fmt.Sprintf("%s_%d", out[:30], len(out))
	}
	return out
}
