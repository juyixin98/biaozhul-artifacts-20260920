package integration

import (
	"context"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/clearsettle/clearsettle/internal/service"
)

func refundIn(paymentID uuid.UUID, amount int64) service.RefundInput {
	return service.RefundInput{PaymentID: paymentID, AmountCents: amount}
}

func stmtIn(refID uuid.UUID, kind string, amount int64, day time.Time) service.StatementInput {
	return service.StatementInput{RefID: refID, Kind: kind, AmountCents: amount, StmtDate: day}
}

func truncDay(t time.Time) time.Time {
	t = t.UTC()
	y, m, d := t.Date()
	return time.Date(y, m, d, 0, 0, 0, 0, time.UTC)
}

func TestReconciliationCleanAndThreshold(t *testing.T) {
	url, cleanup := startPostgres(t)
	defer cleanup()
	e := newTestEnv(t, url)
	ctx := context.Background()
	m, op, _ := e.createMerchantWithKeys(t, "Recon Clean")

	p1 := e.captureAuth(t, op, m.ID, 5000)
	_, _, err := e.svc.Refund(ctx, op, refundIn(p1.ID, 1000), idem("rc-r1"))
	require.NoError(t, err)
	p2 := e.captureAuth(t, op, m.ID, 3000)
	_ = p2

	day := e.clock.Now()
	n, err := e.svc.SyncGatewayStatements(ctx, e.admin, m.ID, day)
	require.NoError(t, err)
	assert.Equal(t, 3, n) // 2 captures + 1 refund

	res, err := e.svc.Reconcile(ctx, e.admin, m.ID, day)
	require.NoError(t, err)
	assert.Equal(t, int32(0), res.Run.DiscrepanciesCount, "perfect feed reconciles clean")
	assert.Equal(t, int64(8000), res.Run.CapturedCents)
	assert.Equal(t, int64(1000), res.Run.RefundedCents)
	assert.Equal(t, int64(7000), res.Run.ExpectedCashCents)
	// Reconciliation settles first. Fees: p1 175 - 29 released = 146;
	// p2 117 -> fees kept 263; net paid out 7000-263 = 6737, so the
	// gateway cash balance is just the retained fees (263).
	assert.Equal(t, int64(263), res.Run.GatewayCashCents)
	assert.Equal(t, int64(0), res.Run.DiffCents,
		"double-entry identity gateway+fee+payable nets to zero")

	// Re-run is idempotent: same run, no duplicate discrepancies.
	res2, err := e.svc.Reconcile(ctx, e.admin, m.ID, day)
	require.NoError(t, err)
	assert.True(t, res2.AlreadyExisted)
	assert.Equal(t, res.Run.ID, res2.Run.ID)
	assert.Len(t, res2.Discrepancies, len(res.Discrepancies))
}

func TestReconciliationDiscrepancies(t *testing.T) {
	url, cleanup := startPostgres(t)
	defer cleanup()
	e := newTestEnv(t, url)
	ctx := context.Background()
	m, op, _ := e.createMerchantWithKeys(t, "Recon Dirty")

	p := e.captureAuth(t, op, m.ID, 4000)
	day := e.clock.Now()

	// Sync the clean feed, then corrupt one statement by 100 cents (> 5).
	_, err := e.svc.SyncGatewayStatements(ctx, e.admin, m.ID, day)
	require.NoError(t, err)
	require.NoError(t, e.svc.ImportStatementRow(ctx, e.admin, m.ID, stmtIn(p.ID, "capture", 3900, day)))

	res, err := e.svc.Reconcile(ctx, e.admin, m.ID, day)
	require.NoError(t, err)
	require.Equal(t, int32(1), res.Run.DiscrepanciesCount)
	assert.Equal(t, "amount_mismatch", res.Discrepancies[0].Kind)
	assert.Equal(t, int64(100), res.Discrepancies[0].DiffCents)

	// A 5-cent difference is tolerated; 6 cents records.
	m2, op2, _ := e.createMerchantWithKeys(t, "Recon Threshold")
	p2 := e.captureAuth(t, op2, m2.ID, 2000)
	_, err = e.svc.SyncGatewayStatements(ctx, e.admin, m2.ID, day)
	require.NoError(t, err)
	require.NoError(t, e.svc.ImportStatementRow(ctx, e.admin, m2.ID, stmtIn(p2.ID, "capture", 1995, day)))
	res2, err := e.svc.Reconcile(ctx, e.admin, m2.ID, day)
	require.NoError(t, err)
	assert.Equal(t, int32(0), res2.Run.DiscrepanciesCount, "5 cent difference is within tolerance")
}

func TestReconciliationInterruptedRecovery(t *testing.T) {
	url, cleanup := startPostgres(t)
	defer cleanup()
	e := newTestEnv(t, url)
	ctx := context.Background()
	m, op, _ := e.createMerchantWithKeys(t, "Recon Crash")
	e.captureAuth(t, op, m.ID, 6000)
	day := e.clock.Now()
	_, err := e.svc.SyncGatewayStatements(ctx, e.admin, m.ID, day)
	require.NoError(t, err)

	// Simulate a run left 'running' with stale discrepancy rows by inserting
	// directly, then re-running: stale rows must be rewritten, never doubled.
	require.NoError(t, e.svc.ExecRaw(ctx, `
		INSERT INTO reconciliation_runs (merchant_id, run_date, status)
		VALUES ($1, $2, 'running')`, m.ID, truncDay(day)))
	var runID uuid.UUID
	require.NoError(t, e.svc.QueryRowRaw(ctx,
		`SELECT id FROM reconciliation_runs WHERE merchant_id=$1 AND run_date=$2`,
		m.ID, truncDay(day)).Scan(&runID))
	require.NoError(t, e.svc.ExecRaw(ctx, `
		INSERT INTO discrepancies (run_id, kind, expected_cents, actual_cents, diff_cents, detail)
		VALUES ($1, 'ledger_balance', 1, 2, 1, 'stale')`, runID))

	res, err := e.svc.Reconcile(ctx, e.admin, m.ID, day)
	require.NoError(t, err)
	assert.Equal(t, "done", res.Run.Status)

	// Exactly the current discrepancies remain (clean feed => none).
	var count int
	require.NoError(t, e.svc.QueryRowRaw(ctx,
		`SELECT count(*) FROM discrepancies WHERE run_id=$1`, runID).Scan(&count))
	assert.Equal(t, 0, count)
}
