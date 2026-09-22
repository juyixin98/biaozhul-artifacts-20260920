// Package tests contains end-to-end integration tests for DAMS.
//
// They require a real PostgreSQL instance. Set DAMS_TEST_DATABASE_URL, or use
// the helper scripts/run-tests.sh which starts a disposable container.
package tests

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"net/http/httptest"
	"os"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"dams.local/dams/internal/migrate"
	"dams.local/dams/internal/server"
)

type testEnv struct {
	pool *pgxpool.Pool
	srv  *httptest.Server
}

func newTestEnv(t *testing.T) *testEnv {
	t.Helper()
	url := testDatabaseURL()
	if url == "" {
		t.Skip("DAMS_TEST_DATABASE_URL not set; use scripts/run-tests.sh")
	}
	ctx := context.Background()

	adminConn, err := pgx.Connect(ctx, url)
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	// Each test process run starts clean.
	_, _ = adminConn.Exec(ctx, `DROP SCHEMA public CASCADE; CREATE SCHEMA public;`)
	if err := migrate.EnsureSchema(ctx, adminConn); err != nil {
		_ = adminConn.Close(ctx)
		t.Fatalf("migrate: %v", err)
	}
	_ = adminConn.Close(ctx)

	pool, err := pgxpool.New(ctx, url)
	if err != nil {
		t.Fatalf("pool: %v", err)
	}
	t.Cleanup(pool.Close)

	srv := httptest.NewServer(server.New(pool).Router())
	t.Cleanup(srv.Close)

	return &testEnv{pool: pool, srv: srv}
}

func testDatabaseURL() string {
	return os.Getenv("DAMS_TEST_DATABASE_URL")
}

type tokens struct {
	Admin   string
	Analyst string
	Auditor string
}

// seedOrg creates an organization with one user per role and returns their
// bearer tokens. slug must be unique per test.
func (e *testEnv) seedOrg(t *testing.T, slug, tz string) tokens {
	t.Helper()
	ctx := context.Background()
	var tk tokens
	var orgID int64
	err := e.pool.QueryRow(ctx,
		`INSERT INTO organizations (slug, name, timezone) VALUES ($1,$1,$2) RETURNING id`,
		slug, tz).Scan(&orgID)
	if err != nil {
		t.Fatalf("create org: %v", err)
	}

	create := func(email, role string) string {
		var uid int64
		err := e.pool.QueryRow(ctx,
			`INSERT INTO users (email, display_name) VALUES ($1,$1) RETURNING id`, email).Scan(&uid)
		if err != nil {
			t.Fatalf("create user: %v", err)
		}
		_, err = e.pool.Exec(ctx,
			`INSERT INTO memberships (org_id,user_id,role) VALUES ($1,$2,$3)`,
			orgID, uid, role)
		if err != nil {
			t.Fatalf("membership: %v", err)
		}
		token := fmt.Sprintf("dams_test_%s_%s_%d", slug, role, time.Now().UnixNano())
		sum := sha256.Sum256([]byte(token))
		_, err = e.pool.Exec(ctx,
			`INSERT INTO api_tokens (user_id, token_hash) VALUES ($1,$2)`,
			uid, hex.EncodeToString(sum[:]))
		if err != nil {
			t.Fatalf("token: %v", err)
		}
		return token
	}

	tk.Admin = create(slug+"-admin@test.local", "admin")
	tk.Analyst = create(slug+"-analyst@test.local", "analyst")
	tk.Auditor = create(slug+"-auditor@test.local", "auditor")
	return tk
}

func (e *testEnv) orgID(t *testing.T, slug string) int64 {
	t.Helper()
	var id int64
	err := e.pool.QueryRow(context.Background(),
		`SELECT id FROM organizations WHERE slug=$1`, slug).Scan(&id)
	if err != nil {
		t.Fatalf("org id: %v", err)
	}
	return id
}

func (e *testEnv) eventCount(t *testing.T, orgID int64) int {
	t.Helper()
	var n int
	err := e.pool.QueryRow(context.Background(),
		`SELECT count(*) FROM events WHERE org_id=$1`, orgID).Scan(&n)
	if err != nil {
		t.Fatalf("count events: %v", err)
	}
	return n
}
