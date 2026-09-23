package integration

import (
	"context"
	"fmt"
	"strings"
	"sync"
	"testing"

	"costlens/internal/auth"
	"costlens/internal/service"
)

const csvHeader = "account_code,resource_code,service,cost_date,currency,amount\n"

func csvLine(acct, res, svc, date, cur, amt string) string {
	return fmt.Sprintf("%s,%s,%s,%s,%s,%s\n", acct, res, svc, date, cur, amt)
}

func billsFor(acct string, rows [][5]string) string {
	var sb strings.Builder
	sb.WriteString(csvHeader)
	for _, r := range rows {
		sb.WriteString(csvLine(acct, r[0], r[1], r[2], r[3], r[4]))
	}
	return sb.String()
}

func importCSV(ctx context.Context, t *testing.T, svc *service.Service, p *auth.Principal, org, csv string) *service.ImportResult {
	t.Helper()
	res, err := svc.Import(ctx, p, org, "f.csv", strings.NewReader(csv))
	if err != nil {
		t.Fatalf("import: %v", err)
	}
	return res
}

func importExpectErr(ctx context.Context, t *testing.T, svc *service.Service, p *auth.Principal, org, csv string) *service.ValidationError {
	t.Helper()
	_, err := svc.Import(ctx, p, org, "f.csv", strings.NewReader(csv))
	if err == nil {
		t.Fatalf("expected import error, got success")
	}
	ve, ok := err.(*service.ValidationError)
	if !ok {
		t.Fatalf("expected ValidationError, got %T: %v", err, err)
	}
	return ve
}

// TestConcurrentDuplicateImport runs the same batch from many goroutines.
// Exactly one must insert; the rest see duplicates; totals must be counted once.
func TestConcurrentDuplicateImport(t *testing.T) {
	ctx := context.Background()
	td := newTestDB(t)
	f := newFixture(ctx, t, td.pool)
	svc := service.New(td.pool)
	an := principalFor(ctx, t, td.pool, f.analyst1Key)

	csv := billsFor("a1", [][5]string{
		{"r1", "EC2", "2026-03-01", "USD", "10.00"},
		{"r2", "S3", "2026-03-01", "USD", "20.00"},
		{"r1", "EC2", "2026-03-02", "USD", "10.00"},
	})

	const n = 8
	var wg sync.WaitGroup
	results := make([]*service.ImportResult, n)
	errs := make([]error, n)
	wg.Add(n)
	for i := 0; i < n; i++ {
		go func(i int) {
			defer wg.Done()
			results[i], errs[i] = svc.Import(ctx, an, f.org1, "f.csv", strings.NewReader(csv))
		}(i)
	}
	wg.Wait()

	inserted, dups := 0, 0
	for i := 0; i < n; i++ {
		if errs[i] != nil {
			t.Fatalf("worker %d: %v", i, errs[i])
		}
		inserted += results[i].InsertedRowCount
		dups += results[i].DuplicateRowCount
	}
	if inserted != 3 {
		t.Fatalf("total inserted across %d workers = %d, want 3", n, inserted)
	}
	if dups != 3*(n-1) {
		t.Fatalf("duplicates = %d, want %d", dups, 3*(n-1))
	}

	// Monthly total counted once: 40 USD.
	var total string
	err := td.pool.QueryRow(ctx,
		`SELECT total::text FROM monthly_cost_center_summary
		 WHERE cost_center_id=$1 AND currency='USD' AND period='2026-03-01'`, f.cc1).Scan(&total)
	if err != nil {
		t.Fatal(err)
	}
	if total != "40.000000" {
		t.Fatalf("monthly total = %s want 40.000000 (no double counting)", total)
	}
}

// TestBatchRollbackOnConflict verifies that a conflicting row in the middle of
// a batch rolls back the whole batch, including earlier valid rows.
func TestBatchRollbackOnConflict(t *testing.T) {
	ctx := context.Background()
	td := newTestDB(t)
	f := newFixture(ctx, t, td.pool)
	svc := service.New(td.pool)
	an := principalFor(ctx, t, td.pool, f.analyst1Key)

	good := billsFor("a1", [][5]string{{"r1", "EC2", "2026-04-01", "USD", "10.00"}})
	importCSV(ctx, t, svc, an, f.org1, good)

	// Second batch: one new valid row, then a same-key/different-amount row.
	bad := billsFor("a1", [][5]string{
		{"r2", "S3", "2026-04-01", "USD", "5.00"},
		{"r1", "EC2", "2026-04-01", "USD", "99.00"},
	})
	ve := importExpectErr(ctx, t, svc, an, f.org1, bad)
	if ve.Code != "content_conflict" || ve.Line != 3 {
		t.Fatalf("got code=%s line=%d want content_conflict/3", ve.Code, ve.Line)
	}

	// The valid first row of the failed batch must have rolled back.
	var n int
	err := td.pool.QueryRow(ctx, `SELECT count(*) FROM costs WHERE resource_id IN
		(SELECT id FROM resources WHERE code='r2')`).Scan(&n)
	if err != nil {
		t.Fatal(err)
	}
	if n != 0 {
		t.Fatalf("r2 rows = %d, want 0 (batch must roll back)", n)
	}
	// Monthly total stays at 10, not 15.
	var total string
	err = td.pool.QueryRow(ctx, `SELECT total::text FROM monthly_cost_center_summary
		WHERE cost_center_id=$1 AND period='2026-04-01' AND currency='USD'`, f.cc1).Scan(&total)
	if err != nil {
		t.Fatal(err)
	}
	if total != "10.000000" {
		t.Fatalf("monthly total = %s want 10.000000", total)
	}

	// A failed-import audit row exists with the line number.
	var status string
	var line int
	err = td.pool.QueryRow(ctx,
		`SELECT status, error_line FROM cost_imports WHERE status='failed' ORDER BY created_at DESC LIMIT 1`).
		Scan(&status, &line)
	if err != nil {
		t.Fatal(err)
	}
	if status != "failed" || line != 3 {
		t.Fatalf("audit row status=%s line=%d", status, line)
	}
}

// TestResourceServiceMismatchConflict: once a resource code exists with one
// service, a later row with the same code and a different service is a conflict.
func TestResourceServiceMismatchConflict(t *testing.T) {
	ctx := context.Background()
	td := newTestDB(t)
	f := newFixture(ctx, t, td.pool)
	svc := service.New(td.pool)
	an := principalFor(ctx, t, td.pool, f.analyst1Key)

	importCSV(ctx, t, svc, an, f.org1, billsFor("a1", [][5]string{
		{"rshared", "EC2", "2026-07-01", "USD", "10.00"},
	}))
	ve := importExpectErr(ctx, t, svc, an, f.org1, billsFor("a1", [][5]string{
		{"rshared", "Lambda", "2026-07-02", "USD", "10.00"},
	}))
	if ve.Code != "content_conflict" {
		t.Fatalf("code=%s want content_conflict", ve.Code)
	}

	// Direct insert with wrong service is also blocked by the DB trigger.
	_, err := td.pool.Exec(ctx, `
		INSERT INTO costs(import_id,account_id,resource_id,service,cost_date,currency,amount)
		SELECT ci.id, a.id, r.id, 'Lambda', '2026-07-03', 'USD', 1
		FROM cost_imports ci, accounts a, resources r
		WHERE a.code='a1' AND r.code='rshared' LIMIT 1`)
	if err == nil {
		t.Fatal("expected trigger to reject mismatched service insert")
	}
}
