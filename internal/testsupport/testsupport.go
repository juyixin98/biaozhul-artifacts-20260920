// Package testsupport provides PostgreSQL-backed test isolation: each test
// gets its own schema in a shared database so blocks and channels built by
// independent tests never collide.
package testsupport

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"net/url"
	"os"
	"testing"

	"inbox/internal/store"
)

// BaseDSN returns the administrator DSN used to create test schemas.
func BaseDSN() string {
	return envOr("INBOX_TEST_DSN", "postgres://inbox:inbox@localhost:55432/inbox?sslmode=disable")
}

// Harness is an isolated, migrated database schema.
type Harness struct {
	T      *testing.T
	Store  *store.Store
	Schema string
}

// New creates a fresh schema and migrates it. The test is skipped when
// PostgreSQL is not reachable.
func New(t *testing.T) *Harness {
	t.Helper()
	schema := NewSchemaOnly(t)
	st, err := store.New(context.Background(), SchemaDSN(schema))
	if err != nil {
		t.Fatalf("open test pool: %v", err)
	}
	if err := st.Migrate(context.Background()); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	return &Harness{T: t, Store: st, Schema: schema}
}

// NewSchemaOnly provisions an empty schema (no migration). The subprocess
// crash test owns the pool and runs its own migration on startup.
func NewSchemaOnly(t *testing.T) string {
	t.Helper()
	ctx := context.Background()
	admin, err := store.New(ctx, BaseDSN())
	if err != nil {
		t.Skipf("postgresql not available (%v); set INBOX_TEST_DSN to run integration tests", err)
	}
	defer admin.Close()
	schema := "t_" + randSuffix(8)
	if _, err := admin.Pool.Exec(ctx, "CREATE SCHEMA "+schema); err != nil {
		t.Fatalf("create schema: %v", err)
	}
	t.Cleanup(func() {
		adm, err := store.New(context.Background(), BaseDSN())
		if err != nil {
			return
		}
		defer adm.Close()
		_, _ = adm.Pool.Exec(context.Background(), "DROP SCHEMA "+schema+" CASCADE")
	})
	return schema
}

// SchemaDSN returns a DSN pinned to the given schema.
func SchemaDSN(schema string) string { return withSearchPath(BaseDSN(), schema) }

func withSearchPath(dsn, schema string) string {
	u, err := url.Parse(dsn)
	if err != nil {
		return dsn
	}
	q := u.Query()
	q.Set("search_path", schema)
	u.RawQuery = q.Encode()
	return u.String()
}

func randSuffix(n int) string {
	b := make([]byte, n)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}
