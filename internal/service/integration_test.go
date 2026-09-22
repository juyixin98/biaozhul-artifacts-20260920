package service_test

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jmoiron/sqlx"
	_ "github.com/lib/pq"

	"domainengine/internal/clock"
	"domainengine/internal/migrate"
	"domainengine/internal/service"
)

// Integration tests run against a real PostgreSQL (schema is migrated from
// scratch for each test). Set TEST_DATABASE_URL, e.g.:
//
//	docker compose up -d db
//	TEST_DATABASE_URL="postgres://postgres:postgres@localhost:5432/domains?sslmode=disable" go test ./...
const (
	adminID   = "11111111-1111-1111-1111-111111111111"
	resellerA = "22222222-2222-2222-2222-222222222222"
	resellerB = "33333333-3333-3333-3333-333333333333"
	aliceID   = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
	carolID   = "cccccccc-cccc-cccc-cccc-cccccccccccc"
)

var (
	alice = &service.User{ID: aliceID, Role: service.RoleCustomer, ResellerID: strPtr(resellerA)}
	carol = &service.User{ID: carolID, Role: service.RoleCustomer, ResellerID: strPtr(resellerB)}
	admin = &service.User{ID: adminID, Role: service.RoleAdmin}
)

func strPtr(s string) *string { return &s }

var t0 = time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)

type fixture struct {
	svc *service.Service
	clk *clock.Fake
	db  *sqlx.DB
	ctx context.Context
}

func newFixture(t *testing.T) *fixture {
	t.Helper()
	dsn := os.Getenv("TEST_DATABASE_URL")
	if dsn == "" {
		t.Skip("TEST_DATABASE_URL not set; skipping integration test")
	}
	db, err := sqlx.Connect("postgres", dsn)
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	t.Cleanup(func() { db.Close() })
	ctx := context.Background()
	if err := migrate.Up(ctx, db); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	// Clean slate.
	if _, err := db.ExecContext(ctx, `
		TRUNCATE domains, transfers, ledger_entries, credit_holds,
		         idempotency_keys, domain_events, prices, users RESTART IDENTITY CASCADE`); err != nil {
		t.Fatalf("truncate: %v", err)
	}
	// Fixtures: users, prices (integer cents), seed credits.
	if _, err := db.ExecContext(ctx, `
		INSERT INTO users (id, api_key, role, name, reseller_id) VALUES
			($1, 'admin-key', 'admin', 'Admin', NULL),
			($2, 'ra-key', 'reseller', 'Reseller A', NULL),
			($3, 'rb-key', 'reseller', 'Reseller B', NULL),
			($4, 'alice-key', 'customer', 'Alice', $2),
			($5, 'carol-key', 'customer', 'Carol', $3)`,
		adminID, resellerA, resellerB, aliceID, carolID); err != nil {
		t.Fatalf("seed users: %v", err)
	}
	if _, err := db.ExecContext(ctx, `
		INSERT INTO prices (tld, action, amount_cents, effective_from) VALUES
			('com', 'register', 1000, '2020-01-01'),
			('com', 'renew',    1000, '2020-01-01'),
			('com', 'transfer', 1000, '2020-01-01'),
			('com', 'redeem',   5000, '2020-01-01')`); err != nil {
		t.Fatalf("seed prices: %v", err)
	}
	if _, err := db.ExecContext(ctx, `
		INSERT INTO ledger_entries (reseller_id, idempotency_key, kind, amount_cents, memo) VALUES
			($1, 'seed:a', 'credit', 100000, 'seed'),
			($2, 'seed:b', 'credit', 100000, 'seed')`, resellerA, resellerB); err != nil {
		t.Fatalf("seed credits: %v", err)
	}

	clk := clock.NewFake(t0)
	svc := service.New(db, clk, service.Config{}, []byte("0123456789abcdef0123456789abcdef"))
	return &fixture{svc: svc, clk: clk, db: db, ctx: ctx}
}

func (f *fixture) balance(t *testing.T, resellerID string) int64 {
	t.Helper()
	b, err := f.svc.ResellerBalance(f.ctx, resellerID)
	if err != nil {
		t.Fatal(err)
	}
	return b.BalanceCents
}

func (f *fixture) available(t *testing.T, resellerID string) int64 {
	t.Helper()
	b, err := f.svc.ResellerBalance(f.ctx, resellerID)
	if err != nil {
		t.Fatal(err)
	}
	return b.AvailableCents
}

func (f *fixture) domain(t *testing.T, name string) *service.Domain {
	t.Helper()
	d, err := f.svc.GetDomain(f.ctx, admin, name)
	if err != nil {
		t.Fatalf("get domain %s: %v", name, err)
	}
	return d
}

func (f *fixture) ledgerCharges(t *testing.T, resellerID string) []int64 {
	t.Helper()
	var amounts []int64
	if err := f.db.SelectContext(f.ctx, &amounts,
		`SELECT amount_cents FROM ledger_entries WHERE reseller_id = $1 AND kind = 'charge' ORDER BY id`, resellerID); err != nil {
		t.Fatal(err)
	}
	return amounts
}

func mustRegister(t *testing.T, f *fixture, u *service.User, name string, years int) service.Domain {
	t.Helper()
	status, body, err := f.svc.Register(f.ctx, u, name, years, "reg-"+name)
	if err != nil {
		t.Fatalf("register %s: %v", name, err)
	}
	if status != 201 {
		t.Fatalf("register %s: status %d", name, status)
	}
	return body.(service.Domain)
}

// --- concurrency: racing registrations -------------------------------------

func TestConcurrentRegistrationSingleWinner(t *testing.T) {
	f := newFixture(t)
	const n = 8
	var wg sync.WaitGroup
	statuses := make([]int, n)
	errs := make([]error, n)
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			status, _, err := f.svc.Register(f.ctx, alice, "Race-Name.COM.", 1, fmt.Sprintf("race-%d", i))
			statuses[i] = status
			errs[i] = err
		}(i)
	}
	wg.Wait()

	wins := 0
	for i := range statuses {
		if errs[i] == nil && statuses[i] == 201 {
			wins++
		} else if errs[i] != service.ErrDomainTaken {
			t.Errorf("goroutine %d: unexpected result status=%d err=%v", i, statuses[i], errs[i])
		}
	}
	if wins != 1 {
		t.Fatalf("expected exactly 1 winner, got %d", wins)
	}
	// Exactly one charge, no double ownership.
	if got := f.balance(t, resellerA); got != 100000-1000 {
		t.Errorf("balance = %d, want %d (exactly one charge)", got, 100000-1000)
	}
	if d := f.domain(t, "race-name.com"); d.OwnerID != aliceID {
		t.Errorf("owner = %s, want alice", d.OwnerID)
	}
}

// --- idempotency -------------------------------------------------------------

func TestIdempotentRetryReturnsOriginal(t *testing.T) {
	f := newFixture(t)
	status1, body1, err := f.svc.Register(f.ctx, alice, "idem.com", 1, "key-1")
	if err != nil || status1 != 201 {
		t.Fatalf("first register: status=%d err=%v", status1, err)
	}
	// Retry with the same key: the stored response is replayed (as raw JSON)
	// and the operation is not re-executed.
	status2, body2, err := f.svc.Register(f.ctx, alice, "idem.com", 1, "key-1")
	if err != nil || status2 != 201 {
		t.Fatalf("retry: status=%d err=%v", status2, err)
	}
	raw, ok := body2.(json.RawMessage)
	if !ok {
		t.Fatalf("replayed body type = %T, want json.RawMessage", body2)
	}
	if !strings.Contains(string(raw), body1.(service.Domain).ID) {
		t.Errorf("replayed body %s does not contain original domain id", raw)
	}
	if got := f.balance(t, resellerA); got != 100000-1000 {
		t.Errorf("balance = %d, want single charge", got)
	}
}

func TestFailureNotRecordedAndNotCharged(t *testing.T) {
	f := newFixture(t)
	// Drain most of reseller A's balance, leaving 5000 cents.
	if _, err := f.db.ExecContext(f.ctx,
		`INSERT INTO ledger_entries (reseller_id, idempotency_key, kind, amount_cents, memo)
		 VALUES ($1, 'drain', 'charge', 95000, 'drain')`, resellerA); err != nil {
		t.Fatal(err)
	}
	// A 10-year registration costs 10000 > 5000 available: must fail.
	_, _, err := f.svc.Register(f.ctx, alice, "nope.com", 10, "fail-key")
	if err == nil {
		t.Fatal("expected insufficient credits error")
	}
	// Nothing charged, nothing stored, domain absent.
	if got := f.balance(t, resellerA); got != 5000 {
		t.Errorf("balance = %d, want 5000 (failure must not charge)", got)
	}
	if _, err := f.svc.GetDomain(f.ctx, admin, "nope.com"); err != service.ErrNotFound {
		t.Errorf("domain should not exist after failed registration: %v", err)
	}
	// The failed key was not recorded: retrying it after a top-up re-executes.
	if _, err := f.db.ExecContext(f.ctx,
		`INSERT INTO ledger_entries (reseller_id, idempotency_key, kind, amount_cents, memo)
		 VALUES ($1, 'topup', 'credit', 100000, 'topup')`, resellerA); err != nil {
		t.Fatal(err)
	}
	status, _, err := f.svc.Register(f.ctx, alice, "nope.com", 10, "fail-key")
	if err != nil || status != 201 {
		t.Fatalf("retry after top-up: status=%d err=%v", status, err)
	}
}

// --- renewal boundaries ------------------------------------------------------

func TestRenewalBoundaries(t *testing.T) {
	f := newFixture(t)
	mustRegister(t, f, alice, "life.com", 1)
	expires := t0.AddDate(1, 0, 0)

	// Renew just before expiry: extends from the old expiry, renew price.
	f.clk.Set(expires.Add(-time.Second))
	status, _, err := f.svc.Renew(f.ctx, alice, "life.com", 1, "renew-1")
	if err != nil || status != 200 {
		t.Fatalf("renew before expiry: status=%d err=%v", status, err)
	}
	if d := f.domain(t, "life.com"); !d.ExpiresAt.Equal(expires.AddDate(1, 0, 0)) {
		t.Errorf("expires_at = %v, want %v", d.ExpiresAt, expires.AddDate(1, 0, 0))
	}

	// At exactly expires_at the domain expires (boundary is inclusive).
	expires = expires.AddDate(1, 0, 0)
	f.clk.Set(expires)
	rep, err := f.svc.RunMaintenance(f.ctx)
	if err != nil {
		t.Fatal(err)
	}
	if rep.Expired != 1 || f.domain(t, "life.com").Status != service.StatusExpired {
		t.Fatalf("expected expiry at boundary, report=%+v", rep)
	}

	// Renewal in `expired` still costs the renew price and rebases to now.
	if _, _, err := f.svc.Renew(f.ctx, alice, "life.com", 1, "renew-2"); err != nil {
		t.Fatalf("renew in expired: %v", err)
	}
	if got := f.balance(t, resellerA); got != 100000-3000 {
		t.Errorf("balance = %d, want register+2 renewals charged", got)
	}

	// Into redemption: renew there costs the redeem price.
	expires = f.domain(t, "life.com").ExpiresAt
	f.clk.Set(expires.Add(time.Second))
	if _, err := f.svc.RunMaintenance(f.ctx); err != nil {
		t.Fatal(err)
	}
	f.clk.Advance(31 * 24 * time.Hour) // past the 30-day expired grace
	if _, err := f.svc.RunMaintenance(f.ctx); err != nil {
		t.Fatal(err)
	}
	if d := f.domain(t, "life.com"); d.Status != service.StatusRedemption {
		t.Fatalf("status = %s, want redemption", d.Status)
	}
	if _, _, err := f.svc.Renew(f.ctx, alice, "life.com", 1, "renew-3"); err != nil {
		t.Fatalf("redeem: %v", err)
	}
	if got := f.balance(t, resellerA); got != 100000-3000-5000 {
		t.Errorf("balance = %d, want redeem price charged", got)
	}

	// Through pending_delete to deletion; the name becomes available again.
	expires = f.domain(t, "life.com").ExpiresAt
	f.clk.Set(expires.Add(time.Second))
	f.svc.RunMaintenance(f.ctx) // expired
	f.clk.Advance(31 * 24 * time.Hour)
	f.svc.RunMaintenance(f.ctx) // redemption
	f.clk.Advance(31 * 24 * time.Hour)
	f.svc.RunMaintenance(f.ctx) // pending_delete
	if d := f.domain(t, "life.com"); d.Status != service.StatusPendingDelete {
		t.Fatalf("status = %s, want pending_delete", d.Status)
	}
	if _, _, err := f.svc.Renew(f.ctx, alice, "life.com", 1, "renew-4"); err == nil {
		t.Fatal("renewal in pending_delete must fail")
	}
	f.clk.Advance(6 * 24 * time.Hour)
	f.svc.RunMaintenance(f.ctx) // deleted
	if _, err := f.svc.GetDomain(f.ctx, admin, "life.com"); err != service.ErrNotFound {
		t.Fatalf("domain should be deleted: %v", err)
	}
	if _, _, err := f.svc.Register(f.ctx, carol, "life.com", 1, "reg-life-2"); err != nil {
		t.Fatalf("name should be available again: %v", err)
	}
}

// --- renewal vs cleanup race -------------------------------------------------

func TestRenewVsMaintenanceRaceDeterministic(t *testing.T) {
	f := newFixture(t)
	mustRegister(t, f, alice, "race-renew.com", 1)
	f.clk.Advance(366 * 24 * time.Hour) // past expiry

	var wg sync.WaitGroup
	wg.Add(2)
	var renewErr error
	go func() {
		defer wg.Done()
		_, _, renewErr = f.svc.Renew(f.ctx, alice, "race-renew.com", 1, "race-renew-key")
	}()
	go func() {
		defer wg.Done()
		_, _ = f.svc.RunMaintenance(f.ctx)
	}()
	wg.Wait()
	if renewErr != nil {
		t.Fatalf("renew: %v", renewErr)
	}
	// Deterministic outcome regardless of order: registered, exactly one charge.
	if d := f.domain(t, "race-renew.com"); d.Status != service.StatusRegistered {
		t.Errorf("status = %s, want registered", d.Status)
	}
	if charges := f.ledgerCharges(t, resellerA); len(charges) != 2 { // register + one renew
		t.Errorf("charges = %v, want exactly register+renew", charges)
	}
	// Maintenance re-run is a no-op now.
	rep, err := f.svc.RunMaintenance(f.ctx)
	if err != nil {
		t.Fatal(err)
	}
	if rep.Expired != 0 {
		t.Errorf("re-run expired %d domains, want 0", rep.Expired)
	}
}

// --- transfers ---------------------------------------------------------------

func TestTransferLifecycle(t *testing.T) {
	f := newFixture(t)
	mustRegister(t, f, alice, "shop.com", 1)
	code, err := f.svc.GenerateAuthCode(f.ctx, alice, "shop.com")
	if err != nil {
		t.Fatal(err)
	}
	if len(code) != 16 {
		t.Fatalf("auth code length = %d, want 16", len(code))
	}

	status, body, err := f.svc.InitiateTransfer(f.ctx, carol, "shop.com", code, "tr-1")
	if err != nil || status != 201 {
		t.Fatalf("initiate: status=%d err=%v", status, err)
	}
	tr := body.(service.Transfer)
	if got := f.available(t, resellerB); got != 100000-1000 {
		t.Errorf("available = %d, want transfer price frozen", got)
	}
	if d := f.domain(t, "shop.com"); d.Status != service.StatusTransferring {
		t.Errorf("status = %s, want transferring", d.Status)
	}

	if _, _, err := f.svc.ApproveTransfer(f.ctx, alice, tr.ID); err != nil {
		t.Fatalf("approve: %v", err)
	}
	// Not yet: the simulated 5-day registry wait has not elapsed.
	f.clk.Advance(4 * 24 * time.Hour)
	rep, _ := f.svc.RunMaintenance(f.ctx)
	if rep.TransfersComplete != 0 {
		t.Fatal("transfer completed before the 5-day wait")
	}
	f.clk.Advance(25 * time.Hour)
	rep, err = f.svc.RunMaintenance(f.ctx)
	if err != nil {
		t.Fatal(err)
	}
	if rep.TransfersComplete != 1 {
		t.Fatalf("report = %+v, want 1 completed transfer", rep)
	}
	d := f.domain(t, "shop.com")
	if d.OwnerID != carolID || d.Status != service.StatusRegistered {
		t.Errorf("owner=%s status=%s, want carol/registered", d.OwnerID, d.Status)
	}
	// Expiry extended by one year from the original expiry (which was > now).
	if want := t0.AddDate(2, 0, 0); !d.ExpiresAt.Equal(want) {
		t.Errorf("expires_at = %v, want %v", d.ExpiresAt, want)
	}
	// Hold captured exactly once: reseller B charged the transfer price.
	if got := f.balance(t, resellerB); got != 100000-1000 {
		t.Errorf("balance B = %d, want transfer price captured", got)
	}
	if got := f.available(t, resellerB); got != 100000-1000 {
		t.Errorf("available B = %d, hold should be gone", got)
	}
}

func TestTransferApprovalTimeoutReleasesHold(t *testing.T) {
	f := newFixture(t)
	mustRegister(t, f, alice, "timeout.com", 1)
	code, _ := f.svc.GenerateAuthCode(f.ctx, alice, "timeout.com")
	_, _, err := f.svc.InitiateTransfer(f.ctx, carol, "timeout.com", code, "tr-timeout")
	if err != nil {
		t.Fatal(err)
	}
	if got := f.available(t, resellerB); got != 100000-1000 {
		t.Fatalf("available = %d, want frozen", got)
	}
	f.clk.Advance(8 * 24 * time.Hour) // approval deadline is 7 days
	rep, err := f.svc.RunMaintenance(f.ctx)
	if err != nil {
		t.Fatal(err)
	}
	if rep.TransfersTimedOut != 1 {
		t.Fatalf("report = %+v, want 1 timed-out transfer", rep)
	}
	if got := f.balance(t, resellerB); got != 100000 {
		t.Errorf("balance B = %d, want fully released", got)
	}
	if got := f.available(t, resellerB); got != 100000 {
		t.Errorf("available B = %d, want no open holds", got)
	}
	if d := f.domain(t, "timeout.com"); d.OwnerID != aliceID || d.Status != service.StatusRegistered {
		t.Errorf("owner=%s status=%s, want alice/registered", d.OwnerID, d.Status)
	}
}

func TestTransferCancelReleasesHold(t *testing.T) {
	f := newFixture(t)
	mustRegister(t, f, alice, "cancel.com", 1)
	code, _ := f.svc.GenerateAuthCode(f.ctx, alice, "cancel.com")
	_, body, err := f.svc.InitiateTransfer(f.ctx, carol, "cancel.com", code, "tr-cancel")
	if err != nil {
		t.Fatal(err)
	}
	tr := body.(service.Transfer)
	if _, _, err := f.svc.CancelTransfer(f.ctx, carol, tr.ID, "cancel"); err != nil {
		t.Fatalf("cancel: %v", err)
	}
	if got := f.available(t, resellerB); got != 100000 {
		t.Errorf("available B = %d, want hold released", got)
	}
	if d := f.domain(t, "cancel.com"); d.Status != service.StatusRegistered {
		t.Errorf("status = %s, want registered", d.Status)
	}
}

func TestTransferRejectsBadAuthCodeAndSelfTransfer(t *testing.T) {
	f := newFixture(t)
	mustRegister(t, f, alice, "secure.com", 1)
	f.svc.GenerateAuthCode(f.ctx, alice, "secure.com")
	if _, _, err := f.svc.InitiateTransfer(f.ctx, carol, "secure.com", "WRONGWRONGWRONG1", "tr-bad"); err != service.ErrInvalidAuthCode {
		t.Errorf("wrong code: err = %v, want ErrInvalidAuthCode", err)
	}
	code, _ := f.svc.RevealAuthCode(f.ctx, alice, "secure.com")
	if _, _, err := f.svc.InitiateTransfer(f.ctx, alice, "secure.com", code, "tr-self"); err == nil {
		t.Error("self-transfer should fail")
	}
	// Non-owner cannot reveal the code.
	if _, err := f.svc.RevealAuthCode(f.ctx, carol, "secure.com"); err != service.ErrForbidden {
		t.Errorf("reveal by non-owner: err = %v, want forbidden", err)
	}
}

// --- price changes -----------------------------------------------------------

func TestPriceChangeDoesNotRewriteAccepted(t *testing.T) {
	f := newFixture(t)
	mustRegister(t, f, alice, "old-price.com", 1) // 1000

	if err := f.svc.SetPrice(f.ctx, "com", service.ActionRegister, 2000, nil); err != nil {
		t.Fatal(err)
	}
	mustRegister(t, f, alice, "new-price.com", 1) // 2000

	charges := f.ledgerCharges(t, resellerA)
	if len(charges) != 2 || charges[0] != 1000 || charges[1] != 2000 {
		t.Fatalf("charges = %v, want [1000 2000]", charges)
	}

	// A transfer accepted at the old price completes at the old price even
	// after the transfer price changes.
	mustRegister(t, f, alice, "moving.com", 1) // 2000 now
	code, _ := f.svc.GenerateAuthCode(f.ctx, alice, "moving.com")
	_, body, err := f.svc.InitiateTransfer(f.ctx, carol, "moving.com", code, "tr-price")
	if err != nil {
		t.Fatal(err)
	}
	tr := body.(service.Transfer)
	if tr.PriceCents != 1000 {
		t.Fatalf("transfer price = %d, want 1000 (price at acceptance)", tr.PriceCents)
	}
	if err := f.svc.SetPrice(f.ctx, "com", service.ActionTransfer, 5000, nil); err != nil {
		t.Fatal(err)
	}
	if _, _, err := f.svc.ApproveTransfer(f.ctx, alice, tr.ID); err != nil {
		t.Fatal(err)
	}
	f.clk.Advance(6 * 24 * time.Hour)
	if _, err := f.svc.RunMaintenance(f.ctx); err != nil {
		t.Fatal(err)
	}
	bCharges := f.ledgerCharges(t, resellerB)
	if len(bCharges) != 1 || bCharges[0] != 1000 {
		t.Errorf("reseller B charges = %v, want [1000] (accepted price, not 5000)", bCharges)
	}
}

// --- recovery / re-run safety -------------------------------------------------

func TestMaintenanceRerunIsIdempotent(t *testing.T) {
	f := newFixture(t)
	mustRegister(t, f, alice, "expire-me.com", 1)
	mustRegister(t, f, alice, "transfer-me.com", 1)
	code, _ := f.svc.GenerateAuthCode(f.ctx, alice, "transfer-me.com")
	_, body, _ := f.svc.InitiateTransfer(f.ctx, carol, "transfer-me.com", code, "tr-rerun")
	tr := body.(service.Transfer)
	f.svc.ApproveTransfer(f.ctx, alice, tr.ID)

	f.clk.Advance(400 * 24 * time.Hour) // everything falls due at once

	rep1, err := f.svc.RunMaintenance(f.ctx)
	if err != nil {
		t.Fatal(err)
	}
	if rep1.TransfersComplete != 1 || rep1.Expired != 1 {
		t.Fatalf("first pass = %+v", rep1)
	}
	var ledgerCount1, holdsCaptured1 int
	f.db.GetContext(f.ctx, &ledgerCount1, `SELECT COUNT(1) FROM ledger_entries`)
	f.db.GetContext(f.ctx, &holdsCaptured1, `SELECT COUNT(1) FROM credit_holds WHERE status = 'captured'`)

	// Simulate crash recovery: run the same pass again. Nothing may change.
	rep2, err := f.svc.RunMaintenance(f.ctx)
	if err != nil {
		t.Fatal(err)
	}
	if rep2.TransfersComplete != 0 || rep2.TransfersTimedOut != 0 {
		t.Errorf("second pass = %+v, want no transfer work", rep2)
	}
	var ledgerCount2, holdsCaptured2 int
	f.db.GetContext(f.ctx, &ledgerCount2, `SELECT COUNT(1) FROM ledger_entries`)
	f.db.GetContext(f.ctx, &holdsCaptured2, `SELECT COUNT(1) FROM credit_holds WHERE status = 'captured'`)
	if ledgerCount1 != ledgerCount2 || holdsCaptured1 != holdsCaptured2 {
		t.Errorf("re-run changed ledger (%d->%d) or holds (%d->%d)",
			ledgerCount1, ledgerCount2, holdsCaptured1, holdsCaptured2)
	}
	// Reseller B charged exactly once for the completed transfer.
	if charges := f.ledgerCharges(t, resellerB); len(charges) != 1 || charges[0] != 1000 {
		t.Errorf("reseller B charges = %v, want exactly [1000]", charges)
	}
}
