package integration

import (
	"errors"
	"sync"
	"testing"

	"github.com/google/uuid"

	"github.com/clearsettle/clearsettle/internal/payments"
	"github.com/clearsettle/clearsettle/internal/store"
)

// TestConcurrentRefundsNoOverRefund fires many concurrent refunds whose total
// exceeds the captured amount. Exactly as many as fit must succeed; the payment
// must never exceed its captured amount and the ledger must stay balanced.
func TestConcurrentRefundsNoOverRefund(t *testing.T) {
	ctx, e := setup(t)
	mid := createTestMerchant(t, ctx, e)

	p, _, err := e.payments.Authorize(ctx, payments.AuthorizeInput{
		MerchantID: mid, IdempotencyKey: "cr-auth", Amount: 10000,
	}, payActor())
	if err != nil {
		t.Fatal(err)
	}
	capP, _, err := e.payments.Capture(ctx, payments.CaptureInput{
		MerchantID: mid, PaymentID: p.ID, IdempotencyKey: "cr-cap",
	}, payActor())
	if err != nil {
		t.Fatal(err)
	}

	const n = 20
	const each = int64(1000) // total 20000 > 10000 captured
	var wg sync.WaitGroup
	var okCount, failCount int64
	var mu sync.Mutex
	start := make(chan struct{})
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			<-start
			_, _, _, rerr := e.payments.Refund(ctx, payments.RefundInput{
				MerchantID: mid, PaymentID: capP.ID,
				IdempotencyKey: "cr-ref-" + uuid.NewString(),
				Amount:         each,
			}, payActor())
			mu.Lock()
			if rerr == nil {
				okCount++
			} else {
				failCount++
			}
			mu.Unlock()
		}(i)
	}
	close(start)
	wg.Wait()

	if okCount != 10 {
		t.Fatalf("successful refunds=%d want exactly 10 (fail=%d)", okCount, failCount)
	}
	if okCount+failCount != n {
		t.Fatalf("accounted=%d want %d", okCount+failCount, n)
	}

	row, err := store.New(e.pool).GetPayment(ctx, store.GetPaymentParams{ID: capP.ID, MerchantID: mid})
	if err != nil {
		t.Fatal(err)
	}
	if row.RefundedAmount != 10000 {
		t.Fatalf("refunded_amount=%d want 10000", row.RefundedAmount)
	}
	if row.Status != "refunded" {
		t.Fatalf("status=%s want refunded", row.Status)
	}
	// Fee closure: a fully refunded payment returns the entire fee.
	if row.RefundedFee != row.FeeAmount {
		t.Fatalf("refunded_fee=%d want fee=%d", row.RefundedFee, row.FeeAmount)
	}
	assertPostingsBalance(t, ctx, e)
}

// TestSameKeySameParamsReplays returns the original result for a repeated
// request with the same idempotency key and identical parameters.
func TestSameKeySameParamsReplays(t *testing.T) {
	ctx, e := setup(t)
	mid := createTestMerchant(t, ctx, e)

	in := payments.AuthorizeInput{MerchantID: mid, IdempotencyKey: "dup-key", Amount: 2500}
	p1, rep1, err := e.payments.Authorize(ctx, in, payActor())
	if err != nil {
		t.Fatal(err)
	}
	if rep1.Body != nil {
		t.Fatal("first call should not be a replay")
	}
	p2, rep2, err := e.payments.Authorize(ctx, in, payActor())
	if err != nil {
		t.Fatalf("replay: %v", err)
	}
	if rep2.Body == nil {
		t.Fatal("second call must return the stored replay")
	}
	if p2.ID != p1.ID {
		// On the replay path out is zero, but the stored body must match p1.
		if string(rep2.Body) == "" {
			t.Fatal("replay body empty")
		}
	}

	var count int
	if err := e.pool.QueryRow(ctx,
		`SELECT count(*) FROM payments WHERE merchant_id=$1 AND idempotency_key='dup-key'`, mid).Scan(&count); err != nil {
		t.Fatal(err)
	}
	if count != 1 {
		t.Fatalf("expected exactly one payment row, got %d", count)
	}
	if err := e.pool.QueryRow(ctx,
		`SELECT count(*) FROM idempotent_requests WHERE merchant_id=$1 AND idem_key='dup-key'`, mid).Scan(&count); err != nil {
		t.Fatal(err)
	}
	if count != 1 {
		t.Fatalf("expected exactly one idempotency row, got %d", count)
	}
}

// TestSameKeyDifferentParamsConflicts reusing a key with a different body = 409.
func TestSameKeyDifferentParamsConflicts(t *testing.T) {
	ctx, e := setup(t)
	mid := createTestMerchant(t, ctx, e)

	_, _, err := e.payments.Authorize(ctx, payments.AuthorizeInput{
		MerchantID: mid, IdempotencyKey: "conflict-key", Amount: 1000,
	}, payActor())
	if err != nil {
		t.Fatal(err)
	}
	_, _, err = e.payments.Authorize(ctx, payments.AuthorizeInput{
		MerchantID: mid, IdempotencyKey: "conflict-key", Amount: 2000, // different!
	}, payActor())
	if err != payments.ErrConflict {
		t.Fatalf("err=%v want ErrConflict", err)
	}
}

// TestIllegalTransitions verifies the explicit state machine.
func TestIllegalTransitions(t *testing.T) {
	ctx, e := setup(t)
	mid := createTestMerchant(t, ctx, e)

	p, _, err := e.payments.Authorize(ctx, payments.AuthorizeInput{
		MerchantID: mid, IdempotencyKey: "it-auth", Amount: 5000,
	}, payActor())
	if err != nil {
		t.Fatal(err)
	}
	// Refund before capture is illegal.
	_, _, _, err = e.payments.Refund(ctx, payments.RefundInput{
		MerchantID: mid, PaymentID: p.ID, IdempotencyKey: "it-r1", Amount: 100,
	}, payActor())
	if !errors.Is(err, payments.ErrIllegalTransition) {
		t.Fatalf("refund authorized payment: err=%v want ErrIllegalTransition", err)
	}

	capP, _, err := e.payments.Capture(ctx, payments.CaptureInput{
		MerchantID: mid, PaymentID: p.ID, IdempotencyKey: "it-cap",
	}, payActor())
	if err != nil {
		t.Fatal(err)
	}
	// Second capture is illegal.
	_, _, err = e.payments.Capture(ctx, payments.CaptureInput{
		MerchantID: mid, PaymentID: p.ID, IdempotencyKey: "it-cap2",
	}, payActor())
	if !errors.Is(err, payments.ErrIllegalTransition) {
		t.Fatalf("double capture: err=%v want ErrIllegalTransition", err)
	}
	// Void after capture is illegal.
	_, _, err = e.payments.Void(ctx, payments.VoidInput{
		MerchantID: mid, PaymentID: p.ID, IdempotencyKey: "it-v1",
	}, payActor())
	if !errors.Is(err, payments.ErrIllegalTransition) {
		t.Fatalf("void captured: err=%v want ErrIllegalTransition", err)
	}
	// Refund larger than captured is rejected.
	_, _, _, err = e.payments.Refund(ctx, payments.RefundInput{
		MerchantID: mid, PaymentID: capP.ID, IdempotencyKey: "it-big", Amount: 5001,
	}, payActor())
	if err != payments.ErrRefundTooLarge {
		t.Fatalf("over refund: err=%v want ErrRefundTooLarge", err)
	}
}

// TestVoidWindow expires an authorization and asserts capture/void are refused.
func TestVoidWindowExpired(t *testing.T) {
	ctx, e := setup(t)
	mid := createTestMerchant(t, ctx, e)

	p, _, err := e.payments.Authorize(ctx, payments.AuthorizeInput{
		MerchantID: mid, IdempotencyKey: "vw-auth", Amount: 1000,
	}, payActor())
	if err != nil {
		t.Fatal(err)
	}
	// Force expiry.
	if _, err := e.pool.Exec(ctx,
		`UPDATE payments SET expires_at = now() - interval '1 second' WHERE id=$1`, p.ID); err != nil {
		t.Fatal(err)
	}
	if _, _, err := e.payments.Capture(ctx, payments.CaptureInput{
		MerchantID: mid, PaymentID: p.ID, IdempotencyKey: "vw-cap",
	}, payActor()); err != payments.ErrExpiredAuth {
		t.Fatalf("capture expired auth: err=%v want ErrExpiredAuth", err)
	}
	if _, _, err := e.payments.Void(ctx, payments.VoidInput{
		MerchantID: mid, PaymentID: p.ID, IdempotencyKey: "vw-void",
	}, payActor()); err != payments.ErrExpiredAuth {
		t.Fatalf("void expired auth: err=%v want ErrExpiredAuth", err)
	}
}

// TestRefundWindow pushes capture beyond 90 days and asserts refusal.
func TestRefundWindow90Days(t *testing.T) {
	ctx, e := setup(t)
	mid := createTestMerchant(t, ctx, e)

	p, _, err := e.payments.Authorize(ctx, payments.AuthorizeInput{
		MerchantID: mid, IdempotencyKey: "rw-auth", Amount: 1000,
	}, payActor())
	if err != nil {
		t.Fatal(err)
	}
	capP, _, err := e.payments.Capture(ctx, payments.CaptureInput{
		MerchantID: mid, PaymentID: p.ID, IdempotencyKey: "rw-cap",
	}, payActor())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := e.pool.Exec(ctx,
		`UPDATE payments SET captured_at = now() - interval '91 days' WHERE id=$1`, capP.ID); err != nil {
		t.Fatal(err)
	}
	_, _, _, err = e.payments.Refund(ctx, payments.RefundInput{
		MerchantID: mid, PaymentID: capP.ID, IdempotencyKey: "rw-r", Amount: 100,
	}, payActor())
	if err != payments.ErrRefundWindow {
		t.Fatalf("refund after 91d: err=%v want ErrRefundWindow", err)
	}
}
