package integration

import (
	"testing"
	"time"
)

// TestReconRecovery simulates a crashed reconciliation: a 'running' row with a
// partial item is left behind; a second run must take over, replace the partial
// items, and complete exactly one run with the full five checks.
func TestReconRecovery(t *testing.T) {
	ctx, e := setup(t)
	mid := createTestMerchant(t, ctx, e)
	day := time.Now().UTC().Add(-24 * time.Hour).Truncate(24 * time.Hour)

	// Seed a stale 'running' run plus one orphaned partial item.
	var runID string
	if err := e.pool.QueryRow(ctx,
		`INSERT INTO reconciliation_runs (merchant_id, run_date, status)
		 VALUES ($1,$2,'running') RETURNING id`, mid, day).Scan(&runID); err != nil {
		t.Fatal(err)
	}
	if _, err := e.pool.Exec(ctx,
		`INSERT INTO reconciliation_items (run_id, check_name, expected, actual, difference, severity, detail)
		 VALUES ($1,'capture_gross',1,2,1,'discrepancy','stale partial from crashed run')`, runID); err != nil {
		t.Fatal(err)
	}

	run, err := e.recon.Run(ctx, mid, day, sysAudit())
	if err != nil {
		t.Fatalf("recon recovery: %v", err)
	}
	if run.Status != "completed" {
		t.Fatalf("status=%s want completed", run.Status)
	}
	if run.RunID.String() != runID {
		t.Fatalf("recovery created a new run %s instead of taking over %s", run.RunID, runID)
	}
	// The stale discrepancy must have been replaced; a merchant with no activity
	// has all-zero matching checks.
	var itemCount int
	var staleExists bool
	if err := e.pool.QueryRow(ctx,
		`SELECT count(*) FROM reconciliation_items WHERE run_id=$1`, run.RunID).Scan(&itemCount); err != nil {
		t.Fatal(err)
	}
	if itemCount != 5 {
		t.Fatalf("items=%d want 5 complete checks", itemCount)
	}
	if err := e.pool.QueryRow(ctx,
		`SELECT EXISTS(SELECT 1 FROM reconciliation_items WHERE run_id=$1 AND detail='stale partial from crashed run')`,
		run.RunID).Scan(&staleExists); err != nil {
		t.Fatal(err)
	}
	if staleExists {
		t.Fatal("stale partial item from crashed run was not replaced")
	}
	var runCount int
	if err := e.pool.QueryRow(ctx,
		`SELECT count(*) FROM reconciliation_runs WHERE merchant_id=$1 AND run_date=$2`,
		mid, day).Scan(&runCount); err != nil {
		t.Fatal(err)
	}
	if runCount != 1 {
		t.Fatalf("run count=%d want 1", runCount)
	}
}
