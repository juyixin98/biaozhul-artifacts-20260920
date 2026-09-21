package integration

import (
	"testing"
	"time"

	"github.com/clearsettle/clearsettle/internal/payments"
)

// TestReconciliationClean runs recon on a normal day and expects zero anomalies.
func TestReconciliationClean(t *testing.T) {
	ctx, e := setup(t)
	mid := createTestMerchant(t, ctx, e)

	p, _, err := e.payments.Authorize(ctx, payments.AuthorizeInput{
		MerchantID: mid, IdempotencyKey: "rc-auth", Amount: 8000,
	}, payActor())
	if err != nil {
		t.Fatal(err)
	}
	capP, _, err := e.payments.Capture(ctx, payments.CaptureInput{
		MerchantID: mid, PaymentID: p.ID, IdempotencyKey: "rc-cap",
	}, payActor())
	if err != nil {
		t.Fatal(err)
	}
	day := time.Now().UTC().Add(-24 * time.Hour)
	backdateCapture(t, ctx, e, capP.ID, day)

	run, err := e.recon.Run(ctx, mid, day, sysAudit())
	if err != nil {
		t.Fatalf("recon: %v", err)
	}
	if run.Discrepancies != 0 {
		t.Fatalf("clean day discrepancies=%d items=%+v", run.Discrepancies, run.Items)
	}

	// Re-run: must reuse, no duplicate items.
	run2, err := e.recon.Run(ctx, mid, day, sysAudit())
	if err != nil {
		t.Fatalf("recon rerun: %v", err)
	}
	if !run2.Reused {
		t.Fatal("second recon run must reuse the completed run row")
	}
	var items int
	if err := e.pool.QueryRow(ctx,
		`SELECT count(*) FROM reconciliation_items WHERE run_id=$1`, run.RunID).Scan(&items); err != nil {
		t.Fatal(err)
	}
	if items != run2.Checks {
		t.Fatalf("recon items=%d checks=%d (duplicate items created)", items, run2.Checks)
	}
}

// TestReconciliationDiscrepancy tampers with the simulated channel feed (an
// external source of truth) so gross differs by 10 cents; recon must flag it.
func TestReconciliationDiscrepancy(t *testing.T) {
	ctx, e := setup(t)
	mid := createTestMerchant(t, ctx, e)

	p, _, err := e.payments.Authorize(ctx, payments.AuthorizeInput{
		MerchantID: mid, IdempotencyKey: "rd-auth", Amount: 9000,
	}, payActor())
	if err != nil {
		t.Fatal(err)
	}
	capP, _, err := e.payments.Capture(ctx, payments.CaptureInput{
		MerchantID: mid, PaymentID: p.ID, IdempotencyKey: "rd-cap",
	}, payActor())
	if err != nil {
		t.Fatal(err)
	}
	day := time.Now().UTC().Add(-24 * time.Hour)
	backdateCapture(t, ctx, e, capP.ID, day)

	// Channel claims it captured 10 cents more than our ledger records.
	if _, err := e.pool.Exec(ctx,
		`UPDATE channel_events SET gross_amount = gross_amount + 10
		 WHERE payment_id=$1 AND event_type='capture'`, capP.ID); err != nil {
		t.Fatal(err)
	}

	run, err := e.recon.Run(ctx, mid, day, sysAudit())
	if err != nil {
		t.Fatalf("recon: %v", err)
	}
	if run.Discrepancies == 0 {
		t.Fatal("expected at least one discrepancy, got none")
	}
	var found bool
	for _, it := range run.Items {
		if it.Check == "capture_gross" && it.Severity == "discrepancy" && it.Difference == 10 {
			found = true
		}
	}
	if !found {
		t.Fatalf("expected capture_gross discrepancy of +10, got %+v", run.Items)
	}
}

// TestReconciliationFiveCentThreshold pins the tolerance: exactly 5 cents is a
// match, 6 cents is a discrepancy.
func TestReconciliationFiveCentThreshold(t *testing.T) {
	ctx, e := setup(t)

	for _, tc := range []struct {
		delta   int64
		discrep bool
		name    string
	}{
		{5, false, "5-cents-match"},
		{6, true, "6-cents-discrepancy"},
		{-5, false, "minus-5-match"},
		{-6, true, "minus-6-discrepancy"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			mid := createTestMerchant(t, ctx, e)
			p, _, err := e.payments.Authorize(ctx, payments.AuthorizeInput{
				MerchantID: mid, IdempotencyKey: "thr-auth-" + tc.name, Amount: 6000,
			}, payActor())
			if err != nil {
				t.Fatal(err)
			}
			capP, _, err := e.payments.Capture(ctx, payments.CaptureInput{
				MerchantID: mid, PaymentID: p.ID, IdempotencyKey: "thr-cap-" + tc.name,
			}, payActor())
			if err != nil {
				t.Fatal(err)
			}
			day := time.Now().UTC().Add(-24 * time.Hour)
			backdateCapture(t, ctx, e, capP.ID, day)

			if _, err := e.pool.Exec(ctx,
				`UPDATE channel_events SET gross_amount = gross_amount + $2
				 WHERE payment_id=$1 AND event_type='capture'`, capP.ID, tc.delta); err != nil {
				t.Fatal(err)
			}
			run, err := e.recon.Run(ctx, mid, day, sysAudit())
			if err != nil {
				t.Fatalf("recon: %v", err)
			}
			hasCapDiscrep := false
			for _, it := range run.Items {
				if it.Check == "capture_gross" && it.Severity == "discrepancy" {
					hasCapDiscrep = true
				}
			}
			if hasCapDiscrep != tc.discrep {
				t.Fatalf("delta=%d discrepancy=%v want %v (items=%+v)", tc.delta, hasCapDiscrep, tc.discrep, run.Items)
			}
		})
	}
}
