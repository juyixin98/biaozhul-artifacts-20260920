package engine_test

import (
	"context"
	"sync"
	"testing"

	"domainengine/internal/apierror"
	"domainengine/internal/domains"
	"domainengine/internal/models"
	"domainengine/internal/testsupport"
)

// TestConcurrentRushRegistration: N goroutines race to register the same
// canonical name using case/trailing-dot variants. Exactly one must win and be
// charged exactly once.
func TestConcurrentRushRegistration(t *testing.T) {
	e := testsupport.NewEnv(t)
	e.Reset(t)
	ctx := context.Background()

	r1 := e.MkReseller(t, "rush-a", 1_000_000)
	r2 := e.MkReseller(t, "rush-b", 1_000_000)
	c1 := e.MkCustomer(t, r1, "ca")
	c2 := e.MkCustomer(t, r2, "cb")

	variants := []string{"Race.Example.COM", "race.example.com.", "RACE.EXAMPLE.COM", "race.example.com"}
	const n = 16
	var wg sync.WaitGroup
	var mu sync.Mutex
	success, conflict := 0, 0
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			reseller, cust := r1, c1
			if i%2 == 0 {
				reseller, cust = r2, c2
			}
			_, err := e.Domains.Register(ctx, e.Clock, domains.RegisterRequest{
				Name: variants[i%len(variants)], CustomerID: cust,
				ResellerID: reseller, Years: 1,
			})
			mu.Lock()
			defer mu.Unlock()
			switch {
			case err == nil:
				success++
			case isAPI(err, apierror.ErrDomainTaken):
				conflict++
			default:
				t.Errorf("unexpected error: %v", err)
			}
		}(i)
	}
	wg.Wait()

	if success != 1 {
		t.Fatalf("expected exactly 1 successful registration, got %d (conflicts=%d)", success, conflict)
	}
	if success+conflict != n {
		t.Fatalf("all attempts must succeed-or-conflict, got %d/%d", success+conflict, n)
	}
	// Only one domain row exists under the canonical name.
	d, err := e.Domains.Get(ctx, "race.example.com")
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	if d.Status != models.StatusRegistered {
		t.Fatalf("status = %s", d.Status)
	}
	// Exactly one register charge in the whole system.
	var charges int
	if err := e.DB.GetContext(ctx, &charges,
		`SELECT count(*) FROM billing_transactions WHERE kind='register'`); err != nil {
		t.Fatal(err)
	}
	if charges != 1 {
		t.Fatalf("expected 1 register charge, got %d", charges)
	}
}

// TestRegisterIdempotency: concurrent and sequential retries with the same key
// return the same transaction id and never charge twice.
func TestRegisterIdempotency(t *testing.T) {
	e := testsupport.NewEnv(t)
	e.Reset(t)
	ctx := context.Background()
	r := e.MkReseller(t, "idem", 100_000)
	c := e.MkCustomer(t, r, "c")
	key := "register-key-1"

	const n = 8
	results := make([]int64, n)
	var wg sync.WaitGroup
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			res, err := e.Domains.Register(ctx, e.Clock, domains.RegisterRequest{
				Name: "idem.example.com", CustomerID: c, ResellerID: r,
				Years: 1, IdempotencyKey: &key,
			})
			if err != nil {
				t.Errorf("retry %d: %v", i, err)
				return
			}
			results[i] = res.ChargeTx.ID
		}(i)
	}
	wg.Wait()
	for _, id := range results {
		if id != results[0] || id == 0 {
			t.Fatalf("idempotent retries returned differing tx ids: %v", results)
		}
	}
	if got := e.Reseller(t, r).BalanceCents; got != 100_000-1200 {
		t.Fatalf("balance = %d, want %d (single charge)", got, 100_000-1200)
	}
}

// TestRegisterInsufficientFunds: a charge that cannot be afforded fails and
// creates neither a domain nor any ledger movement.
func TestRegisterInsufficientFunds(t *testing.T) {
	e := testsupport.NewEnv(t)
	e.Reset(t)
	ctx := context.Background()
	r := e.MkReseller(t, "poor", 500) // price is 1200
	c := e.MkCustomer(t, r, "c")
	_, err := e.Domains.Register(ctx, e.Clock, domains.RegisterRequest{
		Name: "poor.example.com", CustomerID: c, ResellerID: r, Years: 1,
	})
	if !isAPI(err, apierror.ErrInsufficientFunds) {
		t.Fatalf("want insufficient funds, got %v", err)
	}
	if _, err := e.Domains.Get(ctx, "poor.example.com"); !isAPI(err, apierror.ErrNotFound) {
		t.Fatalf("domain should not exist after failed charge, got %v", err)
	}
	if got := e.Reseller(t, r).BalanceCents; got != 500 {
		t.Fatalf("balance changed after failed charge: %d", got)
	}
	var n int
	e.DB.GetContext(ctx, &n, `SELECT count(*) FROM billing_transactions`)
	if n != 0 {
		t.Fatalf("expected no ledger rows, got %d", n)
	}
}

func isAPI(err error, target *apierror.Error) bool {
	if ae, ok := apierror.As(err); ok {
		return ae.Code == target.Code
	}
	return false
}
