package integration

import (
	"context"
	"testing"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/clearsettle/clearsettle/internal/service"
)

// TestLedgerBalancedAndCorrect verifies double-entry balances and account
// balances through capture, partial refund, settlement and post-settlement
// refund (clawback).
func TestLedgerBalancedAndCorrect(t *testing.T) {
	url, cleanup := startPostgres(t)
	defer cleanup()
	e := newTestEnv(t, url)
	ctx := context.Background()
	m, op, _ := e.createMerchantWithKeys(t, "Ledger Co")

	p := e.captureAuth(t, op, m.ID, 10000) // fee 320, payable 9680

	bals := func() map[string]int64 {
		rows, err := e.svc.AccountBalances(ctx, op, m.ID)
		require.NoError(t, err)
		out := map[string]int64{}
		for _, b := range rows {
			out[b.Code] = b.Balance
		}
		return out
	}

	b := bals()
	assert.Equal(t, int64(10000), b["gateway_cash"])
	assert.Equal(t, int64(-320), b["fee_revenue"])
	assert.Equal(t, int64(-9680), b["payable"])
	assert.Equal(t, int64(0), b["unsettled"])

	// Partial refund 3000 before settlement: fee release = 87
	// (2.9% of 10000 = 290; 2.9% of 7000 = 203; delta 87).
	_, _, err := e.svc.Refund(ctx, op, service.RefundInput{
		PaymentID: p.ID, AmountCents: 3000,
	}, idem("led-r1"))
	require.NoError(t, err)
	b = bals()
	assert.Equal(t, int64(7000), b["gateway_cash"])
	assert.Equal(t, int64(-233), b["fee_revenue"]) // -320+87
	assert.Equal(t, int64(-6767), b["payable"])    // -9680+2913
	assert.Equal(t, int64(0), b["unsettled"])

	// Settle: net 7000-233 = 6767 leaves payable, moves to gateway_cash out.
	res, err := e.svc.SettleDay(ctx, e.admin, m.ID, e.clock.Now())
	require.NoError(t, err)
	assert.Equal(t, int64(7000), res.Batch.TotalCents)
	assert.Equal(t, int64(233), res.Batch.FeeCents)
	assert.Equal(t, int64(6767), res.Batch.NetCents)
	b = bals()
	assert.Equal(t, int64(233), b["gateway_cash"]) // 7000 held less 6767 paid out
	assert.Equal(t, int64(0), b["payable"])

	// Post-settlement refund 2000 (within 90 days): clawback through payable.
	_, _, err = e.svc.Refund(ctx, op, service.RefundInput{
		PaymentID: p.ID, AmountCents: 2000,
	}, idem("led-r2"))
	require.NoError(t, err)
	b = bals()
	// Fee release: cumulative refunded 5000 -> delta 58.
	// gateway_cash: 233-2000 = -1767 (platform paid out more than it kept);
	// payable: 0+1942-... = 1942 positive (merchant owes future settlement).
	assert.Equal(t, int64(-1767), b["gateway_cash"])
	assert.Equal(t, int64(1942), b["payable"])
	assert.Equal(t, int64(-175), b["fee_revenue"]) // -233+58

	// Every referenced entry set balances.
	for _, ref := range []struct {
		typ string
		id  uuid.UUID
	}{
		{"payment", p.ID},
	} {
		entries, err := e.svc.ListLedgerEntries(ctx, op, ref.typ, ref.id)
		require.NoError(t, err)
		var sum int64
		for _, en := range entries {
			sum += en.AmountCents
		}
		assert.Equal(t, int64(0), sum, "entries for %s must balance", ref.typ)
	}
}

// TestLedgerIsImmutable proves UPDATE/DELETE on ledger_entries are rejected.
func TestLedgerIsImmutable(t *testing.T) {
	url, cleanup := startPostgres(t)
	defer cleanup()
	e := newTestEnv(t, url)
	ctx := context.Background()
	m, op, _ := e.createMerchantWithKeys(t, "Immutable Co")
	p := e.captureAuth(t, op, m.ID, 500)

	// Directly exercise the trigger through the pool.
	err := e.svc.ExecRaw(ctx,
		`UPDATE ledger_entries SET amount_cents = 1 WHERE ref_id = $1`, p.ID)
	require.Error(t, err)
	assert.Contains(t, err.Error(), "append-only")

	err = e.svc.ExecRaw(ctx, `DELETE FROM ledger_entries WHERE ref_id = $1`, p.ID)
	require.Error(t, err)
	assert.Contains(t, err.Error(), "append-only")
}

// TestCorrectionPostsReversingEntries verifies corrections are new balanced
// entries routed via suspense and never modify history.
func TestCorrectionPostsReversingEntries(t *testing.T) {
	url, cleanup := startPostgres(t)
	defer cleanup()
	e := newTestEnv(t, url)
	ctx := context.Background()
	m, op, _ := e.createMerchantWithKeys(t, "Correction Co")
	p := e.captureAuth(t, op, m.ID, 1000)
	entriesBefore, err := e.svc.ListLedgerEntries(ctx, e.admin, "payment", p.ID)
	require.NoError(t, err)

	ref, err := e.svc.PostCorrection(ctx, e.admin, service.CorrectionInput{
		MerchantID:  m.ID,
		AccountCode: "gateway_cash",
		AmountCents: -100,
		Reason:      "simulated acquirer short payout",
	})
	require.NoError(t, err)

	corrEntries, err := e.svc.ListLedgerEntries(ctx, e.admin, "manual", ref)
	require.NoError(t, err)
	require.Len(t, corrEntries, 2)
	var sum int64
	for _, en := range corrEntries {
		sum += en.AmountCents
	}
	assert.Equal(t, int64(0), sum)

	entriesAfter, err := e.svc.ListLedgerEntries(ctx, e.admin, "payment", p.ID)
	require.NoError(t, err)
	assert.Equal(t, len(entriesBefore), len(entriesAfter), "history unchanged")
}

// TestSettlementIdempotentAndResumable covers:
//  1. re-running a completed day returns the same batch, no new items;
//  2. a crash after some payments commits resumes without duplicates.
func TestSettlementIdempotentAndResumable(t *testing.T) {
	url, cleanup := startPostgres(t)
	defer cleanup()
	e := newTestEnv(t, url)
	ctx := context.Background()
	m, op, _ := e.createMerchantWithKeys(t, "Settle Co")

	for i := 0; i < 6; i++ {
		e.captureAuth(t, op, m.ID, 1000)
	}
	day := e.clock.Now()

	// First run completes.
	res, err := e.svc.SettleDay(ctx, e.admin, m.ID, day)
	require.NoError(t, err)
	assert.Equal(t, 6, res.ItemsSettled)
	assert.Equal(t, "done", res.Batch.Status)

	// Re-run: nothing new.
	res2, err := e.svc.SettleDay(ctx, e.admin, m.ID, day)
	require.NoError(t, err)
	assert.True(t, res2.AlreadyExisted)
	assert.Equal(t, res.Batch.ID, res2.Batch.ID)
	assert.Equal(t, 0, res2.ItemsSettled)

	// New merchant for the crash-recovery scenario.
	m2, op2, _ := e.createMerchantWithKeys(t, "Settle Crash Co")
	for i := 0; i < 5; i++ {
		e.captureAuth(t, op2, m2.ID, 2000)
	}

	// Cancel the context on the 3rd payment to simulate a killed process.
	crashCtx, cancel := context.WithCancel(ctx)
	var processed int
	e.svc.WithSettleHook(func(_ uuid.UUID) {
		processed++
		if processed == 3 {
			cancel()
		}
	})
	_, err = e.svc.SettleDay(crashCtx, e.admin, m2.ID, day)
	require.Error(t, err, "interrupted run returns an error")
	e.svc.WithSettleHook(nil)

	// Entries for 3 payments are committed; batch still open.
	batch, err := e.svc.ListBatches(ctx, e.admin, m2.ID)
	require.NoError(t, err)
	require.Len(t, batch, 1)
	assert.Equal(t, "open", batch[0].Status)

	// Resume on a fresh context: remaining 2 payments, no duplicates.
	res3, err := e.svc.SettleDay(ctx, e.admin, m2.ID, day)
	require.NoError(t, err)
	assert.Equal(t, 2, res3.ItemsSettled)
	assert.Equal(t, "done", res3.Batch.Status)
	assert.Equal(t, int64(10000), res3.Batch.TotalCents) // 5 * 2000

	// Idempotent again.
	res4, err := e.svc.SettleDay(ctx, e.admin, m2.ID, day)
	require.NoError(t, err)
	assert.True(t, res4.AlreadyExisted)

	// Gateway cash consistency for the crash-recovery merchant:
	// 5 captures of 2000, each fee 88 (58+30) -> net 1912.
	// After settling all 5: gateway_cash = 5*88 = 440 (fees kept), payable 0.
	bals, err := e.svc.AccountBalances(ctx, op2, m2.ID)
	require.NoError(t, err)
	bal := map[string]int64{}
	for _, b := range bals {
		bal[b.Code] = b.Balance
	}
	assert.Equal(t, int64(440), bal["gateway_cash"])
	assert.Equal(t, int64(0), bal["payable"])
}
