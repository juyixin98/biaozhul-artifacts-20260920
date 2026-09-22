package testsupport

import (
	"context"
	"encoding/hex"
	"os"
	"testing"
	"time"

	"domainengine/internal/accounts"
	clk "domainengine/internal/clock"
	"domainengine/internal/codec"
	"domainengine/internal/domains"
	"domainengine/internal/ledger"
	"domainengine/internal/models"
	"domainengine/internal/prices"
	"domainengine/internal/store"
	"domainengine/internal/transfers"

	"github.com/jmoiron/sqlx"
)

// Day is one day in time.Duration units.
const Day = 24 * time.Hour

// Fixed AES-256 key used only by tests.
const TestKeyHex = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

// Env is the fully wired set of services against one test database.
type Env struct {
	DB       *sqlx.DB
	Accounts *accounts.Service
	Prices   *prices.Service
	Ledger   *ledger.Service
	Domains  *domains.Service
	Xfer     *transfers.Service
	Clock    *clk.ManualClock
	Timeline domains.Timeline
	Xcfg     transfers.Config
}

func mustHex(t *testing.T, s string) []byte {
	t.Helper()
	b, err := hex.DecodeString(s)
	if err != nil {
		t.Fatalf("hex: %v", err)
	}
	return b
}

// epoch is a fixed frozen start: 2026-01-01T00:00:00Z.
func epoch() time.Time { return time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC) }

// NewEnv connects, migrates once and wires all services around a manual clock.
func NewEnv(t *testing.T) *Env {
	t.Helper()
	url := os.Getenv("DATABASE_URL")
	if url == "" {
		url = "host=/var/run/postgresql user=admin dbname=domainengine_test sslmode=disable"
	}
	ctx := context.Background()
	db, err := store.EnsureOpen(ctx, url)
	if err != nil {
		t.Skipf("test database unavailable (%v); set DATABASE_URL to run integration tests", err)
	}
	db.SetMaxOpenConns(30)
	if err := store.RunMigrations(ctx, db); err != nil {
		t.Fatalf("migrations: %v", err)
	}
	key := mustHex(t, TestKeyHex)
	cd, err := codec.New(key)
	if err != nil {
		t.Fatalf("codec: %v", err)
	}
	mc := clk.NewManual(epoch())
	tl := domains.Timeline{
		ExpiredGrace:  0,
		RedeemPeriod:  30 * Day,
		PendingDelete: 5 * Day,
	}
	acc := accounts.New(db)
	l := ledger.New(db)
	p := prices.New(db)
	d := domains.New(db, l, p, cd, tl)
	xc := transfers.Config{
		ApprovalWindow: 5 * Day,
		TransferWait:   5 * Day,
		Timeline:       tl,
	}
	x := transfers.New(db, d, p, l, xc)
	return &Env{
		DB: db, Accounts: acc, Prices: p, Ledger: l, Domains: d, Xfer: x,
		Clock: mc, Timeline: tl, Xcfg: xc,
	}
}

// Reset truncates every application table and reseeds the demo prices, giving
// each test an isolated dataset while migrations stay applied.
func (e *Env) Reset(t *testing.T) {
	t.Helper()
	ctx := context.Background()
	_, err := e.DB.ExecContext(ctx, `
		TRUNCATE ledger_entries, billing_transactions, domain_events,
		         transfers, domains, principals, customers, resellers,
		         price_rules RESTART IDENTITY CASCADE`)
	if err != nil {
		t.Fatalf("truncate: %v", err)
	}
	_, err = e.DB.ExecContext(ctx, `
		INSERT INTO price_rules (tld, register_cents, renew_cents, restore_cents, transfer_cents) VALUES
		    ('com', 1200, 1200, 20000, 1200),
		    ('net', 1100, 1100, 20000, 1100),
		    ('org', 1000, 1000, 20000, 1000)`)
	if err != nil {
		t.Fatalf("seed prices: %v", err)
	}
}

// MkReseller creates a reseller with starting credit and returns its id.
func (e *Env) MkReseller(t *testing.T, name string, cents int64) int64 {
	t.Helper()
	r, _, err := e.Accounts.CreateReseller(context.Background(), name, cents)
	if err != nil {
		t.Fatalf("create reseller %s: %v", name, err)
	}
	return r.ID
}

// MkCustomer creates a customer under a reseller and returns its id.
func (e *Env) MkCustomer(t *testing.T, resellerID int64, name string) int64 {
	t.Helper()
	c, _, err := e.Accounts.CreateCustomer(context.Background(), resellerID, name)
	if err != nil {
		t.Fatalf("create customer %s: %v", name, err)
	}
	return c.ID
}

// Reg registers a one-year domain and returns it with the plaintext auth code.
func (e *Env) Reg(t *testing.T, name string, reseller, customer int64) (*models.Domain, string) {
	t.Helper()
	r, err := e.Domains.Register(context.Background(), e.Clock, domains.RegisterRequest{
		Name: name, CustomerID: customer, ResellerID: reseller, Years: 1,
	})
	if err != nil {
		t.Fatalf("register %s: %v", name, err)
	}
	return r.Domain, r.AuthCode
}

// Reseller returns the current balances of a reseller.
func (e *Env) Reseller(t *testing.T, id int64) *models.Reseller {
	t.Helper()
	r, err := e.Accounts.GetReseller(context.Background(), id)
	if err != nil {
		t.Fatalf("get reseller: %v", err)
	}
	return r
}
