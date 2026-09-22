package engine_test

import (
	"context"
	"sync"
	"testing"
	"time"

	"domainengine/internal/apierror"
	"domainengine/internal/domains"
	"domainengine/internal/models"
	"domainengine/internal/testsupport"
)

// TestExpiryLifecycle walks the full clock-driven ladder:
//
//	registered -> expired -> redeemable(30d) -> pending_delete(5d) -> purged
//
// and asserts the name becomes registrable again after purge.
func TestExpiryLifecycle(t *testing.T) {
	e := testsupport.NewEnv(t)
	e.Reset(t)
	ctx := context.Background()
	r := e.MkReseller(t, "life", 1_000_000)
	c := e.MkCustomer(t, r, "c")
	d, _ := e.Reg(t, "life.example.com", r, c)
	expiry := d.ExpiresAt

	// 1ns before expiry: still registered, sweep does nothing.
	e.Clock.Set(expiry.Add(-time.Nanosecond))
	if n, err := e.Domains.Sweep(ctx, e.Clock); err != nil || n != 0 {
		t.Fatalf("pre-expiry sweep n=%d err=%v", n, err)
	}
	assertStatus(t, e, "life.example.com", models.StatusRegistered)

	// Exactly expiry: registered -> expired.
	e.Clock.Set(expiry)
	if _, err := e.Domains.Sweep(ctx, e.Clock); err != nil {
		t.Fatal(err)
	}
	assertStatus(t, e, "life.example.com", models.StatusExpired)

	// 30 days later: expired -> redeemable.
	e.Clock.Set(expiry.Add(30 * testsupport.Day))
	if _, err := e.Domains.Sweep(ctx, e.Clock); err != nil {
		t.Fatal(err)
	}
	assertStatus(t, e, "life.example.com", models.StatusRedeemable)

	// Sweep is idempotent and restart-safe: running it again changes nothing.
	if _, err := e.Domains.Sweep(ctx, e.Clock); err != nil {
		t.Fatal(err)
	}
	assertStatus(t, e, "life.example.com", models.StatusRedeemable)

	// +5 days: redeemable -> pending_delete.
	e.Clock.Set(expiry.Add(35 * testsupport.Day))
	if _, err := e.Domains.Sweep(ctx, e.Clock); err != nil {
		t.Fatal(err)
	}
	assertStatus(t, e, "life.example.com", models.StatusPendingDelete)

	// Renewal and restore are both rejected in pending_delete.
	if _, err := e.Domains.Renew(ctx, e.Clock, domains.RenewRequest{
		Name: "life.example.com", ResellerID: r, Years: 1,
	}); !isAPI(err, apierror.ErrInvalidState) {
		t.Fatalf("renew in pending_delete: want invalid_state, got %v", err)
	}
	if _, err := e.Domains.Restore(ctx, e.Clock, domains.RestoreRequest{
		Name: "life.example.com", ResellerID: r,
	}); !isAPI(err, apierror.ErrInvalidState) {
		t.Fatalf("restore in pending_delete: want invalid_state, got %v", err)
	}

	// +5 more days: purged, row gone, name available.
	e.Clock.Set(expiry.Add(40 * testsupport.Day))
	if _, err := e.Domains.Sweep(ctx, e.Clock); err != nil {
		t.Fatal(err)
	}
	if _, err := e.Domains.Get(ctx, "life.example.com"); !isAPI(err, apierror.ErrNotFound) {
		t.Fatalf("after purge want not found, got %v", err)
	}
	// Purge event was recorded even though the domain row was deleted.
	var purges int
	if err := e.DB.GetContext(ctx, &purges,
		`SELECT count(*) FROM domain_events WHERE event_type='purged'`); err != nil {
		t.Fatal(err)
	}
	if purges != 1 {
		t.Fatalf("want 1 purge event, got %d", purges)
	}

	// Re-registration of the released name succeeds and creates a new row.
	d2, _ := e.Reg(t, "LIFE.example.com.", r, c)
	if d2.ID == d.ID {
		t.Fatal("re-registered name should be a new row")
	}
}

// TestRenewBoundary: renewal is allowed up to the expiry instant (extends from
// the stored expiry, not from now), and rejected once the sweep has expired the
// name.
func TestRenewBoundary(t *testing.T) {
	e := testsupport.NewEnv(t)
	e.Reset(t)
	ctx := context.Background()
	r := e.MkReseller(t, "rb", 1_000_000)
	c := e.MkCustomer(t, r, "c")

	t.Run("renew just before expiry", func(t *testing.T) {
		d, _ := e.Reg(t, "early.example.com", r, c)
		e.Clock.Set(d.ExpiresAt.Add(-time.Second))
		res, err := e.Domains.Renew(ctx, e.Clock, domains.RenewRequest{
			Name: "early.example.com", ResellerID: r, Years: 2,
		})
		if err != nil {
			t.Fatalf("renew: %v", err)
		}
		want := d.ExpiresAt.AddDate(2, 0, 0)
		if !res.Domain.ExpiresAt.Equal(want) {
			t.Fatalf("expiry = %v, want %v (extend from expiry, not now)",
				res.Domain.ExpiresAt, want)
		}
	})

	t.Run("renew after sweep expiry is rejected", func(t *testing.T) {
		d, _ := e.Reg(t, "late.example.com", r, c)
		e.Clock.Set(d.ExpiresAt)
		if _, err := e.Domains.Sweep(ctx, e.Clock); err != nil {
			t.Fatal(err)
		}
		_, err := e.Domains.Renew(ctx, e.Clock, domains.RenewRequest{
			Name: "late.example.com", ResellerID: r, Years: 1,
		})
		if !isAPI(err, apierror.ErrInvalidState) {
			t.Fatalf("want invalid_state after expiry, got %v", err)
		}
	})
}

// TestRenewRacesSweep: at the expiry boundary renewal and the sweep compete.
// Both legal orderings are asserted deterministically first; then a
// concurrent stress run checks the invariants (exactly one outcome per name,
// one charge per renewed name, no charge for a lost renewal).
func TestRenewRacesSweep(t *testing.T) {
	e := testsupport.NewEnv(t)
	e.Reset(t)
	ctx := context.Background()
	r := e.MkReseller(t, "race", 5_000_000)
	c := e.MkCustomer(t, r, "c")

	// Ordering A: renewal commits first; the following sweep sees the extended
	// deadline and leaves the name registered with exactly one charge.
	t.Run("renew before sweep", func(t *testing.T) {
		d, _ := e.Reg(t, "ordera.example.com", r, c)
		e.Clock.Set(d.ExpiresAt)
		before := e.Reseller(t, r).BalanceCents
		res, err := e.Domains.Renew(ctx, e.Clock, domains.RenewRequest{
			Name: d.CanonicalName, ResellerID: r, Years: 1,
		})
		if err != nil {
			t.Fatalf("renew: %v", err)
		}
		if _, err := e.Domains.Sweep(ctx, e.Clock); err != nil {
			t.Fatal(err)
		}
		assertStatus(t, e, d.CanonicalName, models.StatusRegistered)
		if !res.Domain.ExpiresAt.Equal(d.ExpiresAt.AddDate(1, 0, 0)) {
			t.Fatalf("expiry not extended")
		}
		if got := before - e.Reseller(t, r).BalanceCents; got != 1200 {
			t.Fatalf("charged %d, want exactly 1200", got)
		}
	})

	// Ordering B: sweep commits first (name expires); the renewal is rejected
	// and never charges.
	t.Run("sweep before renew", func(t *testing.T) {
		d, _ := e.Reg(t, "orderb.example.com", r, c)
		e.Clock.Set(d.ExpiresAt)
		before := e.Reseller(t, r).BalanceCents
		if _, err := e.Domains.Sweep(ctx, e.Clock); err != nil {
			t.Fatal(err)
		}
		_, err := e.Domains.Renew(ctx, e.Clock, domains.RenewRequest{
			Name: d.CanonicalName, ResellerID: r, Years: 1,
		})
		if !isAPI(err, apierror.ErrInvalidState) {
			t.Fatalf("renew after expiry: want invalid_state, got %v", err)
		}
		assertStatus(t, e, d.CanonicalName, models.StatusExpired)
		if got := before - e.Reseller(t, r).BalanceCents; got != 0 {
			t.Fatalf("lost renewal charged %d, want 0", got)
		}
	})

	t.Run("concurrent stress", func(t *testing.T) {
		startBalance := e.Reseller(t, r).BalanceCents
		var renewBefore int
		if err := e.DB.GetContext(ctx, &renewBefore,
			`SELECT count(*) FROM billing_transactions WHERE kind='renew'`); err != nil {
			t.Fatal(err)
		}

		renewed, expired := 0, 0
		const iters = 12
		for iter := 0; iter < iters; iter++ {
			name := pickName(iter)
			dm, _ := e.Reg(t, name, r, c)
			e.Clock.Set(dm.ExpiresAt)

			var wg sync.WaitGroup
			var renewErr, sweepErr error
			wg.Add(2)
			go func() {
				defer wg.Done()
				_, renewErr = e.Domains.Renew(ctx, e.Clock, domains.RenewRequest{
					Name: name, ResellerID: r, Years: 1,
				})
			}()
			go func() { defer wg.Done(); _, sweepErr = e.Domains.Sweep(ctx, e.Clock) }()
			wg.Wait()
			if sweepErr != nil {
				t.Fatalf("sweep: %v", sweepErr)
			}

			got, _ := e.Domains.Get(ctx, name)
			switch got.Status {
			case models.StatusRegistered:
				if renewErr != nil {
					t.Fatalf("registered but renew errored: %v", renewErr)
				}
				renewed++
			case models.StatusExpired:
				if !isAPI(renewErr, apierror.ErrInvalidState) {
					t.Fatalf("expired but renew err=%v (want invalid_state)", renewErr)
				}
				expired++
			default:
				t.Fatalf("unexpected status %s", got.Status)
			}
		}
		t.Logf("stress outcomes: renewed=%d expired=%d", renewed, expired)

		var renewCharges int
		if err := e.DB.GetContext(ctx, &renewCharges,
			`SELECT count(*) FROM billing_transactions WHERE kind='renew'`); err != nil {
			t.Fatal(err)
		}
		if got := renewCharges - renewBefore; int64(got) != int64(renewed) {
			t.Fatalf("stress renew charges %d != renewed domains %d", got, renewed)
		}
		// startBalance predates only the stress registrations; the winning
		// renewals are the only other movement since then.
		wantBalance := startBalance - int64(iters)*1200 - int64(renewed)*1200
		if got := e.Reseller(t, r).BalanceCents; got != wantBalance {
			t.Fatalf("balance=%d want=%d (renewed=%d)", got, wantBalance, renewed)
		}
	})
}

// TestRestore: expired/redeemable names can be restored for restore_fee + one
// renewal year and get a fresh year from the restore time.
func TestRestore(t *testing.T) {
	e := testsupport.NewEnv(t)
	e.Reset(t)
	ctx := context.Background()
	r := e.MkReseller(t, "res", 5_000_000)
	c := e.MkCustomer(t, r, "c")

	cases := []struct {
		name string
		at   func(expiry time.Time) time.Time
	}{
		{"expired-a.example.com", func(e time.Time) time.Time { return e.Add(time.Hour) }},
		{"expired-b.example.com", func(e time.Time) time.Time { return e.Add(20 * testsupport.Day) }},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			d, _ := e.Reg(t, tc.name, r, c)
			e.Clock.Set(d.ExpiresAt)
			if _, err := e.Domains.Sweep(ctx, e.Clock); err != nil {
				t.Fatal(err)
			}
			restoreAt := tc.at(d.ExpiresAt)
			e.Clock.Set(restoreAt)
			if d.ExpiresAt.Add(30*testsupport.Day).Sub(restoreAt) < 0 {
				if _, err := e.Domains.Sweep(ctx, e.Clock); err != nil {
					t.Fatal(err)
				} // into redeemable
			}
			before := e.Reseller(t, r).BalanceCents
			res, err := e.Domains.Restore(ctx, e.Clock, domains.RestoreRequest{
				Name: tc.name, ResellerID: r,
			})
			if err != nil {
				t.Fatalf("restore: %v", err)
			}
			wantExpiry := restoreAt.AddDate(1, 0, 0)
			if !res.Domain.ExpiresAt.Equal(wantExpiry) {
				t.Fatalf("expiry=%v want %v", res.Domain.ExpiresAt, wantExpiry)
			}
			if res.Domain.Status != models.StatusRegistered {
				t.Fatalf("status=%s", res.Domain.Status)
			}
			// restore fee 20000 + one renewal year 1200 = 21200
			if got, want := before-e.Reseller(t, r).BalanceCents, int64(21200); got != want {
				t.Fatalf("charged %d, want %d", got, want)
			}
		})
	}
}

func assertStatus(t *testing.T, e *testsupport.Env, name, want string) {
	t.Helper()
	d, err := e.Domains.Get(context.Background(), name)
	if err != nil {
		t.Fatalf("get %s: %v", name, err)
	}
	if d.Status != want {
		t.Fatalf("%s status=%s want %s", name, d.Status, want)
	}
}

func pickName(i int) string {
	return "b" + string(rune('a'+i%26)) + string(rune('a'+(i/26)%26)) + i2s(i) + ".example.com"
}

func i2s(i int) string {
	if i == 0 {
		return "0"
	}
	var b [8]byte
	p := len(b)
	for i > 0 {
		p--
		b[p] = byte('0' + i%10)
		i /= 10
	}
	return string(b[p:])
}
