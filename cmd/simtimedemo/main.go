// Command simtimedemo runs an end-to-end, fully simulated walkthrough of the
// domain lifecycle engine WITHOUT waiting for real days: it drives an
// injectable manual clock forward while the same worker logic the server runs
// advances states. Nothing outside this process/database is contacted.
//
// Usage:
//
//	# against the docker-compose database (default):
//	go run ./cmd/simtimedemo
//
//	# or with an explicit DSN:
//	DATABASE_URL=postgres://domain:domain@localhost:5432/domain?sslmode=disable \
//	  go run ./cmd/simtimedemo
//
// The demo prints the clock, domain status and ledger balances at each stage.
// It truncates application tables first, so never point it at production data.
package main

import (
	"context"
	"encoding/hex"
	"flag"
	"fmt"
	"log"
	"os"
	"time"

	clk "domainengine/internal/clock"
	"domainengine/internal/codec"
	"domainengine/internal/domains"
	"domainengine/internal/ledger"
	"domainengine/internal/models"
	"domainengine/internal/prices"
	"domainengine/internal/store"
	"domainengine/internal/transfers"
)

func main() {
	dbURL := flag.String("db", envOr("DATABASE_URL",
		"postgres://domain:domain@localhost:5432/domain?sslmode=disable"), "database DSN")
	keyHex := flag.String("key", envOr("CODEC_KEY_HEX",
		"000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"), "AES-256 key hex")
	flag.Parse()

	ctx := context.Background()
	db, err := store.EnsureOpen(ctx, *dbURL)
	must(err)
	defer db.Close()
	must(store.RunMigrations(ctx, db))

	// Fresh demo state.
	for _, table := range []string{
		"ledger_entries", "billing_transactions", "domain_events", "transfers",
		"domains", "principals", "customers", "resellers", "price_rules",
	} {
		_, _ = db.ExecContext(ctx, "TRUNCATE "+table+" RESTART IDENTITY CASCADE")
	}
	_, err = db.ExecContext(ctx, `
		INSERT INTO price_rules (tld, register_cents, renew_cents, restore_cents, transfer_cents)
		VALUES ('com',1200,1200,20000,1200),
		       ('org',1000,1000,20000,1000)`)
	must(err)

	key, err := hex.DecodeString(*keyHex)
	must(err)
	cd, err := codec.New(key)
	must(err)

	// The whole point: a frozen clock we move by hand.
	start := time.Date(2026, 1, 1, 12, 0, 0, 0, time.UTC)
	clock := clk.NewManual(start)

	// Short, demo-friendly timeline. In production these come from config.
	tl := domains.Timeline{
		ExpiredGrace:  0,
		RedeemPeriod:  30 * 24 * time.Hour, // 30-day redemption grace
		PendingDelete: 5 * 24 * time.Hour,
	}
	led := ledger.New(db)
	prs := prices.New(db)
	dom := domains.New(db, led, prs, cd, tl)
	xsvc := transfers.New(db, dom, prs, led, transfers.Config{
		ApprovalWindow: 5 * 24 * time.Hour, // seller has 5 days
		TransferWait:   5 * 24 * time.Hour, // simulated registry wait
		Timeline:       tl,
	})

	// Two resellers.
	var alice, bob int64
	must(db.QueryRowxContext(ctx,
		`INSERT INTO resellers (name, balance_cents) VALUES ('alice',100000) RETURNING id`).Scan(&alice))
	must(db.QueryRowxContext(ctx,
		`INSERT INTO resellers (name, balance_cents) VALUES ('bob',50000) RETURNING id`).Scan(&bob))
	var ac, bc int64
	must(db.QueryRowxContext(ctx,
		`INSERT INTO customers (reseller_id,name) VALUES ($1,'alice-cust') RETURNING id`, alice).Scan(&ac))
	must(db.QueryRowxContext(ctx,
		`INSERT INTO customers (reseller_id,name) VALUES ($1,'bob-cust') RETURNING id`, bob).Scan(&bc))

	stage := func(msg string) {
		fmt.Printf("\n=== [%s] %s ===\n", clock.Now().Format(time.RFC3339), msg)
	}
	printBalance := func(name string, id int64) {
		var r models.Reseller
		must(db.GetContext(ctx, &r, `SELECT * FROM resellers WHERE id=$1`, id))
		fmt.Printf("  %-5s available=%6d cents  held=%5d cents\n", name, r.BalanceCents, r.HeldCents)
	}
	printDomain := func(name string) {
		d, err := dom.Get(ctx, name)
		if err != nil {
			fmt.Printf("  %-18s (released / available)\n", name)
			return
		}
		fmt.Printf("  %-18s status=%-15s ownerReseller=%d expires=%s\n",
			name, d.Status, d.ResellerID, d.ExpiresAt.Format("2006-01-02"))
	}

	// 1. Alice registers demo.com for 1 year (idempotency key supplied).
	stage("alice registers demo.com (1 year, $12.00)")
	regKey := "demo-register-1"
	reg, err := dom.Register(ctx, clock, domains.RegisterRequest{
		Name: "Demo.COM.", CustomerID: ac, ResellerID: alice, Years: 1,
		IdempotencyKey: &regKey,
	})
	must(err)
	fmt.Printf("  canonical name=%s  16-char auth code (shown once)=%s\n",
		reg.Domain.CanonicalName, reg.AuthCode)
	printDomain("demo.com")
	printBalance("alice", alice)

	// 2. Idempotent retry with the same key returns the original result and
	// does not charge again; a genuinely different attempt (no key) is refused
	// by canonical uniqueness without charging.
	retry, err := dom.Register(ctx, clock, domains.RegisterRequest{
		Name: "demo.com", CustomerID: ac, ResellerID: alice, Years: 1,
		IdempotencyKey: &regKey,
	})
	must(err)
	fmt.Printf("  idempotent retry returned same charge tx=%d\n", retry.ChargeTx.ID)
	_, err = dom.Register(ctx, clock, domains.RegisterRequest{
		Name: "DEMO.com.", CustomerID: ac, ResellerID: alice, Years: 1,
	})
	fmt.Printf("  duplicate register attempt -> %v (expected: domain_taken)\n", shortErr(err))
	printBalance("alice", alice)

	// 3. Fast-forward to just before expiry and renew.
	expiry := reg.Domain.ExpiresAt
	clock.Set(expiry.Add(-1 * time.Hour))
	stage("59 minutes before expiry: alice renews for 1 year ($12.00)")
	_, err = dom.Renew(ctx, clock, domains.RenewRequest{
		Name: "demo.com", ResellerID: alice, Years: 1,
	})
	must(err)
	printDomain("demo.com")
	printBalance("alice", alice)

	// 4. Bob starts a transfer using the auth code; fee is frozen.
	stage("bob requests transfer of demo.com (fee $12.00 frozen, not captured)")
	tr, _, err := xsvc.Request(ctx, clock, transfers.RequestInput{
		DomainName: "demo.com", AuthCode: reg.AuthCode,
		ToResellerID: bob, ToCustomerID: bc,
	})
	must(err)
	printDomain("demo.com")
	printBalance("alice", alice)
	printBalance("bob", bob)

	// 5. Alice approves the same day; 5-day simulated wait begins.
	stage("alice approves the transfer; 5-day registry wait begins")
	_, err = xsvc.Approve(ctx, clock, tr.ID, alice)
	must(err)

	clock.Advance(4 * 24 * time.Hour)
	stage("day 4 of the 5-day wait: nothing happens yet")
	n, err := xsvc.ProcessDue(ctx, clock)
	must(err)
	fmt.Printf("  transfers processed=%d\n", n)
	printDomain("demo.com")

	clock.Advance(2 * 24 * time.Hour)
	stage("day 6: wait elapsed -> transfer completes, fee captured, owner = bob")
	n, err = xsvc.ProcessDue(ctx, clock)
	must(err)
	fmt.Printf("  transfers processed=%d\n", n)
	printDomain("demo.com")
	printBalance("alice", alice)
	printBalance("bob", bob)

	// 6. Let the name expire and walk the redemption ladder.
	d2, err := dom.Get(ctx, "demo.com")
	must(err)
	clock.Set(d2.ExpiresAt)
	stage("expiry reached: sweep moves registered -> expired")
	_, err = dom.Sweep(ctx, clock)
	must(err)
	printDomain("demo.com")

	clock.Advance(15 * 24 * time.Hour)
	stage("day 15: still in the expired phase; restore costs fee + 1 year")
	_, err = dom.Sweep(ctx, clock) // stays 'expired': redeemable_at is day 30
	must(err)
	printDomain("demo.com")

	// Restore directly from the expired phase (also allowed in redeemable).
	_, err = dom.Restore(ctx, clock, domains.RestoreRequest{Name: "demo.com", ResellerID: bob})
	must(err)
	fmt.Println("  bob restored the name (restore fee $200.00 + 1 year $12.00)")
	printDomain("demo.com")
	printBalance("bob", bob)

	// 7. Now let a second name go the full distance to release.
	stage("extra: watch release.example.org ride all the way to re-registration")
	reg2, err := dom.Register(ctx, clock, domains.RegisterRequest{
		Name: "release.example.org", CustomerID: ac, ResellerID: alice, Years: 1,
	})
	must(err)
	clock.Set(reg2.Domain.ExpiresAt)
	_, err = dom.Sweep(ctx, clock) // -> expired
	must(err)
	clock.Set(reg2.Domain.ExpiresAt.Add(30 * 24 * time.Hour))
	_, err = dom.Sweep(ctx, clock) // -> redeemable
	must(err)
	clock.Set(reg2.Domain.ExpiresAt.Add(35 * 24 * time.Hour))
	_, err = dom.Sweep(ctx, clock) // -> pending_delete
	must(err)
	printDomain("release.example.org")
	clock.Set(reg2.Domain.ExpiresAt.Add(40 * 24 * time.Hour))
	_, err = dom.Sweep(ctx, clock) // -> released (row deleted)
	must(err)
	printDomain("release.example.org")

	reg3, err := dom.Register(ctx, clock, domains.RegisterRequest{
		Name: "RELEASE.Example.ORG.", CustomerID: bc, ResellerID: bob, Years: 1,
	})
	must(err)
	fmt.Printf("  re-registered released name as new domain id=%d (owner bob)\n", reg3.Domain.ID)

	// 8. Final ledger summary.
	stage("final ledger (append-only, integer cents)")
	rows, err := led.History(ctx, bob, 50)
	must(err)
	for i := len(rows) - 1; i >= 0; i-- {
		r := rows[i]
		fmt.Printf("  tx=%-3d %-17s amount=%6d\n", r.ID, r.Kind, r.AmountCents)
	}

	fmt.Println("\nDemo complete. No real registrar, DNS or payment service was contacted.")
}

func shortErr(err error) string {
	if err == nil {
		return "<nil>"
	}
	return err.Error()
}

func envOr(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}

func must(err error) {
	if err != nil {
		log.Fatalf("demo: %v", err)
	}
}
