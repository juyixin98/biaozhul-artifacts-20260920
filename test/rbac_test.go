package integration

import (
	"context"
	"strings"
	"testing"

	"costlens/internal/service"
)

// TestViewerIsReadOnly: viewers can read scoped data but cannot import.
func TestViewerIsReadOnly(t *testing.T) {
	ctx := context.Background()
	td := newTestDB(t)
	f := newFixture(ctx, t, td.pool)
	svc := service.New(td.pool)
	vw := principalFor(ctx, t, td.pool, f.viewerKey)
	an := principalFor(ctx, t, td.pool, f.analyst1Key)

	importCSV(ctx, t, svc, an, f.org1, billsFor("a1", [][5]string{
		{"r1", "EC2", "2026-01-01", "USD", "10.00"},
	}))

	rows, err := svc.DailyAccount(ctx, vw, []string{f.org1}, service.Filters{})
	if err != nil || len(rows) != 1 {
		t.Fatalf("viewer read: rows=%d err=%v", len(rows), err)
	}

	if _, err := svc.Import(ctx, vw, f.org1, "f.csv",
		strings.NewReader(billsFor("a1", [][5]string{{"r2", "EC2", "2026-01-02", "USD", "10.00"}}))); err == nil {
		t.Fatal("viewer import must fail")
	}
}

// TestAnalystScopeEnforced: an analyst scoped to org2 cannot import into or
// read org1, even supplying its id explicitly.
func TestAnalystScopeEnforced(t *testing.T) {
	ctx := context.Background()
	td := newTestDB(t)
	f := newFixture(ctx, t, td.pool)
	svc := service.New(td.pool)
	an2 := principalFor(ctx, t, td.pool, f.analyst2Key)

	if _, err := svc.Import(ctx, an2, f.org1, "f.csv",
		strings.NewReader(billsFor("a1", [][5]string{{"r1", "EC2", "2026-01-01", "USD", "10.00"}}))); err == nil {
		t.Fatal("cross-org import must fail")
	}

	rows, _, err := svc.ListCosts(ctx, an2, []string{f.org1}, service.Filters{Limit: 100})
	if err != nil || len(rows) != 0 {
		t.Fatalf("cross-org read: rows=%d err=%v (want 0)", len(rows), err)
	}

	// Account code a1 exists only in org1 and is unresolvable under org2.
	ve := importExpectErr(ctx, t, svc, an2, f.org2, billsFor("a1", [][5]string{
		{"r1", "EC2", "2026-01-01", "USD", "10.00"},
	}))
	if ve.Code != "unknown_account" {
		t.Fatalf("code=%s want unknown_account", ve.Code)
	}
}
