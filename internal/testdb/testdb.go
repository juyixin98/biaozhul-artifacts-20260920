// Package testdb provisions an isolated, migrated PostgreSQL database per
// test package, so packages running in parallel never contend on catalog
// tables or on concurrent CREATE TYPE statements during migration.
package testdb

import (
	"context"
	"net/url"
	"os"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"costlens/internal/migrate"
)

// Setup connects to the admin DSN (TEST_DATABASE_URL, default points at
// database "costlens"), creates a fresh uniquely-named database, migrates it
// and returns a pool plus a cleanup func that drops it. A connection failure
// is returned as an error so TestMain can skip integration tests.
func Setup(suffix string) (*pgxpool.Pool, func(), error) {
	adminDSN := envOr("TEST_DATABASE_URL",
		"postgres://costlens:costlens@localhost:55432/costlens?sslmode=disable")

	u, err := url.Parse(adminDSN)
	if err != nil {
		return nil, nil, err
	}
	dbName := "costlens_test_" + suffix
	adminURL := *u
	adminURL.Path = "/postgres"

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	admin, err := pgx.Connect(ctx, adminURL.String())
	if err != nil {
		return nil, nil, err
	}
	defer admin.Close(ctx)
	if _, err := admin.Exec(ctx,
		`DROP DATABASE IF EXISTS `+quoteIdent(dbName)+` WITH (FORCE)`); err != nil {
		return nil, nil, err
	}
	if _, err := admin.Exec(ctx, `CREATE DATABASE `+quoteIdent(dbName)); err != nil {
		return nil, nil, err
	}

	testURL := *u
	testURL.Path = "/" + dbName
	dsn := testURL.String()

	conn, err := pgx.Connect(context.Background(), dsn)
	if err != nil {
		return nil, nil, err
	}
	if err := migrate.Migrate(context.Background(), conn); err != nil {
		conn.Close(context.Background())
		return nil, nil, err
	}
	conn.Close(context.Background())

	pool, err := pgxpool.New(context.Background(), dsn)
	if err != nil {
		return nil, nil, err
	}
	cleanup := func() {
		pool.Close()
		c, err := pgx.Connect(context.Background(), adminURL.String())
		if err == nil {
			_, _ = c.Exec(context.Background(),
				`DROP DATABASE IF EXISTS `+quoteIdent(dbName)+` WITH (FORCE)`)
			c.Close(context.Background())
		}
	}
	return pool, cleanup, nil
}

func quoteIdent(s string) string {
	return `"` + strings.ReplaceAll(s, `"`, `""`) + `"`
}

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}
