package integration

import (
	"context"
	"crypto/rand"
	"fmt"
	"testing"
	"time"

	"costlens/internal/auth"

	"github.com/jackc/pgx/v5/pgxpool"
)

type fixture struct {
	org1, org2                  string
	cc1, cc2                    string
	acct1, acct2, acct3         string
	adminKey, analyst1Key       string
	analyst2Key, viewerKey      string
	analyst1, viewer, adminUser string
}

// newAccount creates an additional account in org1/cc1 with a specific
// created_at (used by the insufficient-history test).
func (f *fixture) newAccount(ctx context.Context, t *testing.T, pool *pgxpool.Pool, code string, created time.Time) string {
	t.Helper()
	var id string
	err := pool.QueryRow(ctx,
		`INSERT INTO accounts(org_id,cost_center_id,code,name,created_at)
		 VALUES ($1,$2,$3,$3,$4) RETURNING id`,
		f.org1, f.cc1, code, created).Scan(&id)
	if err != nil {
		t.Fatal(err)
	}
	return id
}

func countRows(ctx context.Context, t *testing.T, pool *pgxpool.Pool, query string, args ...any) int {
	t.Helper()
	var n int
	if err := pool.QueryRow(ctx, query, args...).Scan(&n); err != nil {
		t.Fatal(err)
	}
	return n
}

func newFixture(ctx context.Context, t *testing.T, pool *pgxpool.Pool) *fixture {
	t.Helper()
	f := &fixture{}

	uid := func() string {
		var b [16]byte
		_, _ = rand.Read(b[:])
		b[6] = (b[6] & 0x0f) | 0x40
		b[8] = (b[8] & 0x3f) | 0x80
		return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:16])
	}
	f.org1, f.org2 = uid(), uid()
	f.cc1, f.cc2 = uid(), uid()
	f.acct1, f.acct2, f.acct3 = uid(), uid(), uid()
	f.adminUser, f.analyst1, f.viewer = uid(), uid(), uid()
	analyst2 := uid()

	must := func(q string, args ...any) {
		if _, err := pool.Exec(ctx, q, args...); err != nil {
			t.Fatalf("fixture: %v\nSQL: %s", err, q)
		}
	}
	must(`INSERT INTO organizations(id,code,name) VALUES ($1,'o1','Org1'),($2,'o2','Org2')`, f.org1, f.org2)
	must(`INSERT INTO cost_centers(id,org_id,code,name) VALUES
		($1,$3,'cc1','CC1'),($2,$4,'cc2','CC2')`, f.cc1, f.cc2, f.org1, f.org2)
	must(`INSERT INTO accounts(id,org_id,cost_center_id,code,name,created_at) VALUES
		($1,$4,$6,'a1','A1','2025-01-01'),
		($2,$4,$6,'a2','A2','2025-01-01'),
		($3,$5,$7,'a3','A3','2025-01-01')`,
		f.acct1, f.acct2, f.acct3, f.org1, f.org2, f.cc1, f.cc2)
	must(`INSERT INTO app_users(id,login,full_name,role) VALUES
		($1,'admin','Ad','admin'),($2,'an1','An','analyst'),
		($3,'an2','An2','analyst'),($4,'vw','Vw','viewer')`,
		f.adminUser, f.analyst1, analyst2, f.viewer)
	must(`INSERT INTO user_org_scopes(user_id,org_id) VALUES
		($1,$3),($2,$3),($4,$5)`,
		f.analyst1, f.viewer, f.org1, analyst2, f.org2)

	f.adminKey = "test-admin-" + uid()
	f.analyst1Key = "test-an1-" + uid()
	f.analyst2Key = "test-an2-" + uid()
	f.viewerKey = "test-vw-" + uid()
	must(`INSERT INTO api_keys(user_id,key_hash,hint) VALUES
		($1,$5,'admin'),($2,$6,'an1'),($3,$7,'an2'),($4,$8,'vw')`,
		f.adminUser, f.analyst1, analyst2, f.viewer,
		auth.HashKey(f.adminKey), auth.HashKey(f.analyst1Key),
		auth.HashKey(f.analyst2Key), auth.HashKey(f.viewerKey))
	return f
}

func principalFor(ctx context.Context, t *testing.T, pool *pgxpool.Pool, key string) *auth.Principal {
	t.Helper()
	p, err := auth.Authenticate(ctx, pool, "Bearer "+key)
	if err != nil {
		t.Fatalf("auth: %v", err)
	}
	return p
}
