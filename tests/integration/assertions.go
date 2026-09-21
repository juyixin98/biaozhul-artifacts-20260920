package integration

import (
	"context"
	"testing"
)

// assertPostingsBalance verifies the global ledger invariant: every posting
// group (ref_type, ref_id) nets to zero.
func assertPostingsBalance(t *testing.T, ctx context.Context, e *env) {
	t.Helper()
	var n int
	err := e.pool.QueryRow(ctx, `SELECT count(*) FROM v_unbalanced_postings`).Scan(&n)
	if err != nil {
		t.Fatalf("check unbalanced postings: %v", err)
	}
	if n != 0 {
		t.Fatalf("found %d unbalanced posting group(s)", n)
	}
}
