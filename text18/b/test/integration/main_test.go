package integration

import (
	"context"
	"fmt"
	"os"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	dblib "sircc/internal/db"
)

const defaultAdminURL = "postgres://sircc:sircc@localhost:5432/postgres?sslmode=disable"

// testDB is provisioned once for the whole package: a fresh database with the
// full migration set. Subtests truncate shared tables as needed.
var (
	adminURL string
	pool     *pgxpool.Pool
)

func TestMain(m *testing.M) {
	adminURL = os.Getenv("TEST_DATABASE_URL")
	if adminURL == "" {
		adminURL = defaultAdminURL
	}

	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()

	adminConn, err := pgx.Connect(ctx, adminURL)
	if err != nil {
		fmt.Fprintf(os.Stderr, "integration tests need PostgreSQL at %s: %v\n(set TEST_DATABASE_URL to override)\n", adminURL, err)
		os.Exit(0) // skip, don't fail, environments without a DB
	}

	name := "sircc_test_" + randomHex(6)
	if _, err := adminConn.Exec(ctx, "CREATE DATABASE "+name); err != nil {
		fmt.Fprintf(os.Stderr, "create test database: %v\n", err)
		os.Exit(1)
	}

	testURL := dbURLWithName(adminURL, name)
	pool, err = pgxpool.New(ctx, testURL)
	if err != nil {
		fmt.Fprintf(os.Stderr, "connect test db: %v\n", err)
		os.Exit(1)
	}

	conn, err := pool.Acquire(ctx)
	if err != nil {
		fmt.Fprintf(os.Stderr, "acquire: %v\n", err)
		os.Exit(1)
	}
	migrationsDir := os.Getenv("TEST_MIGRATIONS_DIR")
	if migrationsDir == "" {
		migrationsDir = "../../migrations"
	}
	if err := dblib.Migrate(ctx, conn.Conn(), migrationsDir); err != nil {
		fmt.Fprintf(os.Stderr, "migrate: %v\n", err)
		os.Exit(1)
	}
	conn.Release()

	code := m.Run()

	pool.Close()
	_, _ = adminConn.Exec(context.Background(), "DROP DATABASE "+name)
	_ = adminConn.Close(context.Background())
	os.Exit(code)
}

func dbURLWithName(url, name string) string {
	// Replace the database segment (between the last '/' and '?').
	slash := -1
	for i := len(url) - 1; i >= 0; i-- {
		if url[i] == '/' {
			slash = i
			break
		}
	}
	if slash < 0 {
		return url + "/" + name
	}
	rest := url[slash+1:]
	q := ""
	for i := 0; i < len(rest); i++ {
		if rest[i] == '?' {
			q = rest[i:]
			rest = rest[:i]
			break
		}
	}
	return url[:slash+1] + name + q
}
