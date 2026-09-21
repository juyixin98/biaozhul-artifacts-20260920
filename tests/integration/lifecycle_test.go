package integration

import (
	"context"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/clearsettle/clearsettle/internal/payments"
)

// TestFullLifecycle drives authorize -> capture -> partial refund -> settle ->
// post-settlement refund and asserts ledger postings balance throughout.
func TestFullLifecycle(t *testing.T) {
	ctx, e := setup(t)
	mid := createTestMerchant(t, ctx, e)

	// Authorize $40.00.
	authP, _, err := e.payments.Authorize(ctx, payments.AuthorizeInput{
		MerchantID: mid, IdempotencyKey: "auth-1", Amount: 4000,
	}, payActor())
	if err != nil {
		t.Fatalf("authorize: %v", err)
	}
	if authP.Status != "authorized" {
		t.Fatalf("status=%s want authorized", authP.Status)
	}

	// Capture the full amount. Fee = 2.9% of 4000 = 116 + 30 = 146.
	capP, _, err := e.payments.Capture(ctx, payments.CaptureInput{
		MerchantID: mid, PaymentID: authP.ID, IdempotencyKey: "cap-1",
	}, payActor())
	if err != nil {
		t.Fatalf("capture: %v", err)
	}
	if capP.FeeAmount != 146 {
		t.Fatalf("fee=%d want 146", capP.FeeAmount)
	}
	if capP.Status != "captured" {
		t.Fatalf("status=%s want captured", capP.Status)
	}

	// Partial refund of $10.00. feeBack = round(146*1000/4000)=37.
	ref, upd, _, err := e.payments.Refund(ctx, payments.RefundInput{
		MerchantID: mid, PaymentID: capP.ID, IdempotencyKey: "ref-1", Amount: 1000,
	}, payActor())
	if err != nil {
		t.Fatalf("refund: %v", err)
	}
	if ref.FeeRefund != 37 {
		t.Fatalf("feeRefund=%d want 37", ref.FeeRefund)
	}
	if upd.Status != "partially_refunded" {
		t.Fatalf("status=%s want partially_refunded", upd.Status)
	}
	if upd.RefundedAmount != 1000 {
		t.Fatalf("refunded=%d want 1000", upd.RefundedAmount)
	}

	// Ledger must still balance overall: cash +4000 -1000 = +3000;
	// fees -146 +37 = -109; payable -(4000-146)+(1000-37) = -3854+963 = -2891.
	assertBalances(t, ctx, e, mid, map[string]int64{
		"CASH":        3000,
		"FEE_REVENUE": -109,
		"PAYABLE":     -2891,
	})

	// Backdate the capture to yesterday so it is settleable, then settle.
	yesterday := time.Now().UTC().Add(-24 * time.Hour)
	backdateCapture(t, ctx, e, capP.ID, yesterday)
	res := mustSettleDay(t, ctx, e, mid, yesterday)
	if res.Skipped {
		t.Fatal("expected a new settlement batch")
	}
	wantNet := int64(4000 - 1000 - 146 + 37) // 2891
	if res.NetAmount != wantNet {
		t.Fatalf("net=%d want %d", res.NetAmount, wantNet)
	}
	if res.PaymentCount != 1 {
		t.Fatalf("payment count=%d want 1", res.PaymentCount)
	}

	// After settlement, payable should be zeroed by the payout posting.
	assertBalances(t, ctx, e, mid, map[string]int64{
		"CASH":        3000 - wantNet, // cash leaves to merchant bank
		"FEE_REVENUE": -109,
		"PAYABLE":     0,
	})

	// Idempotent re-settlement must not create a second batch or postings.
	res2 := mustSettleDay(t, ctx, e, mid, yesterday)
	if !res2.Skipped {
		t.Fatal("re-running settlement must skip an already completed day")
	}
	if res2.NetAmount != wantNet {
		t.Fatalf("re-run net=%d want %d", res2.NetAmount, wantNet)
	}

	// Reconcile yesterday: internal == channel -> no discrepancies.
	run, err := e.recon.Run(ctx, mid, yesterday, sysAudit())
	if err != nil {
		t.Fatalf("recon: %v", err)
	}
	if run.Discrepancies != 0 {
		t.Fatalf("expected no discrepancies, got %d: %+v", run.Discrepancies, run.Items)
	}
}

// backdateCapture moves captured_at (and created_at) onto day so settlement
// and reconciliation treat the payment as belonging to that business day.
func backdateCapture(t *testing.T, ctx context.Context, e *env, paymentID uuid.UUID, day time.Time) {
	t.Helper()
	ts := day.UTC().Truncate(24 * time.Hour).Add(12 * time.Hour)
	_, err := e.pool.Exec(ctx,
		`UPDATE payments SET captured_at=$2, created_at=$2 WHERE id=$1`, paymentID, ts)
	if err != nil {
		t.Fatalf("backdate payment: %v", err)
	}
	// Channel events are stamped "today" at write time; move them too so the
	// simulated acquirer feed agrees on day.
	_, err = e.pool.Exec(ctx,
		`UPDATE channel_events SET event_date=$2 WHERE payment_id=$1`, paymentID, ts)
	if err != nil {
		t.Fatalf("backdate channel events: %v", err)
	}
	_, err = e.pool.Exec(ctx,
		`UPDATE refunds SET created_at=$2 WHERE payment_id=$1`, paymentID, ts)
	if err != nil {
		t.Fatalf("backdate refunds: %v", err)
	}
}

func assertBalances(t *testing.T, ctx context.Context, e *env, mid uuid.UUID, want map[string]int64) {
	t.Helper()
	// Scope to postings tied to this merchant (payment/refund/settlement rows
	// carry merchant_id), since CASH and FEE_REVENUE are global accounts.
	balance := func(code string) int64 {
		var bal int64
		err := e.pool.QueryRow(ctx, `
			SELECT COALESCE(SUM(le.amount),0)
			FROM ledger_entries le
			JOIN ledger_accounts la ON la.id = le.account_id
			WHERE la.code = $1 AND (
				(le.ref_type IN ('payment','refund') AND le.ref_id IN (
					SELECT id FROM payments WHERE merchant_id = $2
					UNION SELECT id FROM refunds WHERE merchant_id = $2))
				OR (le.ref_type = 'settlement' AND le.ref_id IN (
					SELECT id FROM settlement_batches WHERE merchant_id = $2))
			)`, code, mid).Scan(&bal)
		if err != nil {
			t.Fatalf("balance %s: %v", code, err)
		}
		return bal
	}
	got := map[string]int64{
		"CASH":        balance("CASH"),
		"FEE_REVENUE": balance("FEE_REVENUE"),
		"PAYABLE":     balance("PAYABLE:" + mid.String()),
	}
	var grand int64
	for _, v := range got {
		grand += v
	}
	if grand != 0 {
		t.Fatalf("ledger not balanced across accounts: total=%d (%+v)", grand, got)
	}
	for code, w := range want {
		if got[code] != w {
			t.Errorf("account %s balance=%d want %d (all=%+v)", code, got[code], w, got)
		}
	}
}
