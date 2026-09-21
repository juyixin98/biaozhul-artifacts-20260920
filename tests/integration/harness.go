// Package integration contains end-to-end tests that require PostgreSQL.
// They run against TEST_DATABASE_URL (default points at the docker-compose DB
// on port 35432). Each test uses a unique merchant so they can run in parallel.
package integration

import (
	"context"
	"os"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/clearsettle/clearsettle/internal/admin"
	"github.com/clearsettle/clearsettle/internal/audit"
	"github.com/clearsettle/clearsettle/internal/db"
	"github.com/clearsettle/clearsettle/internal/payments"
	"github.com/clearsettle/clearsettle/internal/recon"
	"github.com/clearsettle/clearsettle/internal/settle"
)

const defaultDSN = "postgres://clearsettle:clearsettle@127.0.0.1:35432/clearsettle?sslmode=disable"

type env struct {
	pool     *pgxpool.Pool
	admin    *admin.Service
	payments *payments.Service
	settle   *settle.Service
	recon    *recon.Service
}

func testDSN() string {
	if v := os.Getenv("TEST_DATABASE_URL"); v != "" {
		return v
	}
	return defaultDSN
}

func setup(t *testing.T) (context.Context, *env) {
	t.Helper()
	if testing.Short() {
		t.Skip("skipping integration test in -short mode")
	}
	ctx := context.Background()
	pool, err := pgxpool.New(ctx, testDSN())
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	if err := pool.Ping(ctx); err != nil {
		t.Fatalf("ping: %v (start postgres with: docker compose up -d postgres)", err)
	}
	if _, err := db.Migrate(ctx, pool); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	t.Cleanup(pool.Close)
	return ctx, &env{
		pool:     pool,
		admin:    admin.New(pool),
		payments: payments.New(pool),
		settle:   settle.New(pool, "test-"+t.Name()),
		recon:    recon.New(pool),
	}
}

// createTestMerchant makes an isolated merchant with a fresh API key.
func createTestMerchant(t *testing.T, ctx context.Context, e *env) uuid.UUID {
	t.Helper()
	res, err := e.admin.CreateMerchant(ctx, admin.CreateMerchantInput{
		Name: "test-" + t.Name() + "-" + uuid.NewString()[:8],
	}, admin.Actor{Role: "admin"})
	if err != nil {
		t.Fatalf("create merchant: %v", err)
	}
	return res.Merchant.ID
}

func payActor() payments.Actor { return payments.Actor{Role: "operator"} }
func sysAudit() audit.Entry    { return audit.Entry{ActorRole: "system"} }

func mustSettleDay(t *testing.T, ctx context.Context, e *env, mid uuid.UUID, day time.Time) settle.DayResult {
	t.Helper()
	r, err := e.settle.SettleDay(ctx, mid, day, sysAudit())
	if err != nil {
		t.Fatalf("settle day: %v", err)
	}
	return r
}
