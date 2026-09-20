// Package testsupport provides shared integration-test helpers: a pool
// pointed at the test database, a schema reset and the seeded demo users.
package testsupport

import (
	"context"
	"os"
	"testing"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"

	"sircc/internal/database"
)

// Fixed demo identities seeded by migration 0001.
var (
	AdminID      = uuid.MustParse("11111111-1111-1111-1111-111111111101")
	Analyst1ID   = uuid.MustParse("11111111-1111-1111-1111-111111111201")
	Analyst2ID   = uuid.MustParse("11111111-1111-1111-1111-111111111202")
	Responder1ID = uuid.MustParse("11111111-1111-1111-1111-111111111301")
	Responder2ID = uuid.MustParse("11111111-1111-1111-1111-111111111302")
)

// APIKeys maps a user id to its static demo API key.
var APIKeys = map[uuid.UUID]string{
	AdminID:      "sircc_demo_admin_0001",
	Analyst1ID:   "sircc_demo_analyst_0001",
	Analyst2ID:   "sircc_demo_analyst_0002",
	Responder1ID: "sircc_demo_responder_0001",
	Responder2ID: "sircc_demo_responder_0002",
}

// DatabaseURL returns the test database DSN.
func DatabaseURL() string {
	if v := os.Getenv("SIRCC_TEST_DATABASE_URL"); v != "" {
		return v
	}
	return "postgres://sircc:sircc@127.0.0.1:5432/sircc_test?sslmode=disable"
}

// NewPool opens a connection pool for the test database.
func NewPool(t testing.TB) *pgxpool.Pool {
	t.Helper()
	pool, err := pgxpool.New(context.Background(), DatabaseURL())
	if err != nil {
		t.Fatalf("open pool: %v", err)
	}
	t.Cleanup(pool.Close)
	if err := pool.Ping(context.Background()); err != nil {
		t.Fatalf("ping test db (set SIRCC_TEST_DATABASE_URL): %v", err)
	}
	return pool
}

// ResetDB drops and re-applies the entire schema, giving each test a clean
// database. It is deliberately used per test for full isolation.
func ResetDB(t testing.TB, pool *pgxpool.Pool) {
	t.Helper()
	if err := database.ResetForTests(context.Background(), pool); err != nil {
		t.Fatalf("reset schema: %v", err)
	}
}
