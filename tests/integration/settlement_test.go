package integration

import (
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/clearsettle/clearsettle/internal/payments"
	"github.com/clearsettle/clearsettle/internal/settle"
)

// TestSettlementIdempotentRepeatedRun settles a day, runs it again (and again
// concurrently), and asserts a single batch, a single settlement posting, and
// the same totals every time.
func TestSettlementIdempotentRepeatedRun(t *testing.T) {
	ctx, e := setup(t)
	mid := createTestMerchant(t, ctx, e)

	p, _, err := e.payments.Authorize(ctx, payments.AuthorizeInput{
		MerchantID: mid, IdempotencyKey: "si-auth", Amount: 5000,
	}, payActor())
	if err != nil {
		t.Fatal(err)
	}
	capP, _, err := e.payments.Capture(ctx, payments.CaptureInput{
		MerchantID: mid, PaymentID: p.ID, IdempotencyKey: "si-cap",
	}, payActor())
	if err != nil {
		t.Fatal(err)
	}
	day := time.Now().UTC().Add(-48 * time.Hour)
	backdateCapture(t, ctx, e, capP.ID, day)

	first := mustSettleDay(t, ctx, e, mid, day)
	if first.Skipped {
		t.Fatal("first settlement must not be a skip")
	}
	for i := 0; i < 3; i++ {
		r := mustSettleDay(t, ctx, e, mid, day)
		if !r.Skipped {
			t.Fatalf("settlement run %d duplicated a batch", i+1)
		}
		if r.BatchID != first.BatchID {
			t.Fatalf("run %d created a different batch", i+1)
		}
	}

	var batchCount, postingCount int
	if err := e.pool.QueryRow(ctx,
		`SELECT count(*) FROM settlement_batches WHERE merchant_id=$1 AND batch_date=$2`,
		mid, day.Truncate(24*time.Hour)).Scan(&batchCount); err != nil {
		t.Fatal(err)
	}
	if batchCount != 1 {
		t.Fatalf("batch count=%d want 1", batchCount)
	}
	if err := e.pool.QueryRow(ctx,
		`SELECT count(*) FROM ledger_entries WHERE ref_type='settlement' AND ref_id=$1`,
		first.BatchID).Scan(&postingCount); err != nil {
		t.Fatal(err)
	}
	if postingCount != 2 {
		t.Fatalf("settlement posting lines=%d want 2 (payable + cash)", postingCount)
	}
}

// TestSettlementRecovery simulates a crash after the 'processing' row is created
// but before completion: a second run must take over and finish exactly once.
func TestSettlementRecovery(t *testing.T) {
	ctx, e := setup(t)
	mid := createTestMerchant(t, ctx, e)

	p, _, err := e.payments.Authorize(ctx, payments.AuthorizeInput{
		MerchantID: mid, IdempotencyKey: "sr-auth", Amount: 7000,
	}, payActor())
	if err != nil {
		t.Fatal(err)
	}
	capP, _, err := e.payments.Capture(ctx, payments.CaptureInput{
		MerchantID: mid, PaymentID: p.ID, IdempotencyKey: "sr-cap",
	}, payActor())
	if err != nil {
		t.Fatal(err)
	}
	day := time.Now().UTC().Add(-72 * time.Hour)
	backdateCapture(t, ctx, e, capP.ID, day)

	// Simulate a crashed worker: insert a 'processing' batch row manually.
	var batchID uuid.UUID
	if err := e.pool.QueryRow(ctx,
		`INSERT INTO settlement_batches (merchant_id, batch_date, status, locked_by)
		 VALUES ($1,$2,'processing','crashed-worker') RETURNING id`,
		mid, day.Truncate(24*time.Hour)).Scan(&batchID); err != nil {
		t.Fatal(err)
	}

	r := mustSettleDay(t, ctx, e, mid, day)
	if r.Status != "completed" {
		t.Fatalf("recovery status=%s want completed", r.Status)
	}
	if r.BatchID != batchID {
		t.Fatalf("recovery must take over the existing processing row, got new batch %s (old %s)", r.BatchID, batchID)
	}
	// Payment must be linked to the taken-over batch.
	var linked uuid.UUID
	if err := e.pool.QueryRow(ctx,
		`SELECT settlement_batch_id FROM payments WHERE id=$1`, capP.ID).Scan(&linked); err != nil {
		t.Fatal(err)
	}
	if linked != batchID {
		t.Fatalf("payment linked to %s want recovered batch %s", linked, batchID)
	}
	assertPostingsBalance(t, ctx, e)
}

// TestConcurrentSettlementSameDay runs two workers against the same merchant/day
// simultaneously; only one batch and one payout posting may result.
func TestConcurrentSettlementSameDay(t *testing.T) {
	ctx, e := setup(t)
	mid := createTestMerchant(t, ctx, e)

	p, _, err := e.payments.Authorize(ctx, payments.AuthorizeInput{
		MerchantID: mid, IdempotencyKey: "cs2-auth", Amount: 3000,
	}, payActor())
	if err != nil {
		t.Fatal(err)
	}
	capP, _, err := e.payments.Capture(ctx, payments.CaptureInput{
		MerchantID: mid, PaymentID: p.ID, IdempotencyKey: "cs2-cap",
	}, payActor())
	if err != nil {
		t.Fatal(err)
	}
	day := time.Now().UTC().Add(-96 * time.Hour)
	backdateCapture(t, ctx, e, capP.ID, day)

	res := make(chan settle.DayResult, 2)
	errs := make(chan error, 2)
	go func() {
		r, err := e.settle.SettleDay(ctx, mid, day, sysAudit())
		res <- r
		errs <- err
	}()
	go func() {
		r, err := e.settle.SettleDay(ctx, mid, day, sysAudit())
		res <- r
		errs <- err
	}()
	for i := 0; i < 2; i++ {
		if err := <-errs; err != nil {
			t.Fatalf("concurrent settle: %v", err)
		}
	}
	r1, r2 := <-res, <-res
	if r1.BatchID != r2.BatchID {
		t.Fatalf("two concurrent runs created different batches: %s vs %s", r1.BatchID, r2.BatchID)
	}
	if !(r1.Skipped || r2.Skipped) {
		t.Fatal("one of the two concurrent runs must report a skip")
	}
	var batchCount int
	if err := e.pool.QueryRow(ctx,
		`SELECT count(*) FROM settlement_batches WHERE merchant_id=$1 AND batch_date=$2`,
		mid, day.Truncate(24*time.Hour)).Scan(&batchCount); err != nil {
		t.Fatal(err)
	}
	if batchCount != 1 {
		t.Fatalf("batch count=%d want 1", batchCount)
	}
}
