// Package testdb provisions an isolated PostgreSQL database for test packages.
// Each package gets its own database so `go test ./...` (which runs packages in
// parallel) never contends on shared rows.
package testdb

import (
	"context"
	"fmt"
	"net/url"
	"os"
	"strings"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

const defaultURL = "postgres://community:community@localhost:5440/communityvault?sslmode=disable"

// MigrateFunc applies schema to a freshly created database.
type MigrateFunc func(ctx context.Context, conn *pgx.Conn) error

// Setup connects to (recreating if necessary) database "cv_test_<name>",
// applies migrations, and returns a ready pool.
func Setup(parent context.Context, name string, migrate MigrateFunc) (*pgxpool.Pool, string, error) {
	base := envOr("TEST_DATABASE_URL", defaultURL)
	adminURL, err := withDB(base, "postgres")
	if err != nil {
		return nil, "", err
	}
	dbName := "cv_test_" + name
	if err := recreateDatabase(adminURL, dbName); err != nil {
		return nil, "", err
	}
	target, err := withDB(base, dbName)
	if err != nil {
		return nil, "", err
	}

	conn, err := pgx.Connect(parent, target)
	if err != nil {
		return nil, "", err
	}
	if err := migrate(parent, conn); err != nil {
		conn.Close(parent)
		return nil, "", err
	}
	if err := conn.Close(parent); err != nil {
		return nil, "", err
	}

	pool, err := pgxpool.New(parent, target)
	if err != nil {
		return nil, "", err
	}
	return pool, target, nil
}

func recreateDatabase(adminURL, name string) error {
	ctx := context.Background()
	conn, err := pgx.Connect(ctx, adminURL)
	if err != nil {
		return fmt.Errorf("connect admin db: %w", err)
	}
	defer conn.Close(ctx)
	if _, err := conn.Exec(ctx, "DROP DATABASE IF EXISTS "+name+" WITH (FORCE)"); err != nil {
		return fmt.Errorf("drop db: %w", err)
	}
	if _, err := conn.Exec(ctx, "CREATE DATABASE "+name); err != nil {
		return fmt.Errorf("create db: %w", err)
	}
	return nil
}

// withDB rewrites the database name in a postgres URL.
func withDB(raw, dbName string) (string, error) {
	if strings.HasPrefix(raw, "postgres://") || strings.HasPrefix(raw, "postgresql://") {
		u, err := url.Parse(raw)
		if err != nil {
			return "", err
		}
		u.Path = "/" + dbName
		return u.String(), nil
	}
	if strings.Contains(raw, "dbname=") {
		return strings.ReplaceAll(raw, "dbname=communityvault", "dbname="+dbName), nil
	}
	return raw + " dbname=" + dbName, nil
}

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
