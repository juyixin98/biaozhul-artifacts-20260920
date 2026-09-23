package integration

import (
	"context"
	"strings"
	"sync"
	"testing"
	"time"

	"costlens/internal/service"
)

// TestCrossMonthLateBill: a late bill landing in a previous month recomputes
// only that month's monthly total; the current month is untouched.
func TestCrossMonthLateBill(t *testing.T) {
	ctx := context.Background()
	td := newTestDB(t)
	f := newFixture(ctx, t, td.pool)
	svc := service.New(td.pool)
	an := principalFor(ctx, t, td.pool, f.analyst1Key)

	importCSV(ctx, t, svc, an, f.org1, billsFor("a1", [][5]string{
		{"r1", "EC2", "2026-02-28", "USD", "10.00"},
		{"r1", "EC2", "2026-03-01", "USD", "20.00"},
	}))
	// Late arrival for a previously empty February day.
	importCSV(ctx, t, svc, an, f.org1, billsFor("a1", [][5]string{
		{"r-late", "S3", "2026-02-01", "USD", "5.00"},
	}))

	monthly := func(period string) string {
		var total string
		err := td.pool.QueryRow(ctx, `SELECT total::text FROM monthly_cost_center_summary
			WHERE cost_center_id=$1 AND period=$2 AND currency='USD'`, f.cc1, period).Scan(&total)
		if err != nil {
			t.Fatal(err)
		}
		return total
	}
	if monthly("2026-02-01") != "15.000000" {
		t.Fatalf("feb = %s want 15", monthly("2026-02-01"))
	}
	if monthly("2026-03-01") != "20.000000" {
		t.Fatalf("mar = %s want 20 (must not change)", monthly("2026-03-01"))
	}

	// Rebuild must reproduce exactly the same values.
	if _, err := svc.Rebuild(ctx); err != nil {
		t.Fatal(err)
	}
	if monthly("2026-02-01") != "15.000000" || monthly("2026-03-01") != "20.000000" {
		t.Fatal("rebuild changed cross-month totals")
	}
}

// TestRebuildConcurrentImport interleaves a long rebuild with imports. Because
// both take the same advisory lock, every imported row must appear exactly
// once in the final summaries.
func TestRebuildConcurrentImport(t *testing.T) {
	ctx := context.Background()
	td := newTestDB(t)
	f := newFixture(ctx, t, td.pool)
	svc := service.New(td.pool)
	an := principalFor(ctx, t, td.pool, f.analyst1Key)

	// Seed 60 days of history so rebuild takes measurable time.
	var rows [][5]string
	base := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	for i := 0; i < 60; i++ {
		d := base.AddDate(0, 0, i).Format("2006-01-02")
		rows = append(rows, [5]string{"r" + d, "EC2", d, "USD", "1.00"})
	}
	importCSV(ctx, t, svc, an, f.org1, billsFor("a1", rows))

	var wg sync.WaitGroup
	wg.Add(2)
	go func() { defer wg.Done(); _, _ = svc.Rebuild(ctx) }()
	go func() {
		defer wg.Done()
		for i := 0; i < 10; i++ {
			d := time.Date(2026, 4, 1+i, 0, 0, 0, 0, time.UTC).Format("2006-01-02")
			csv := billsFor("a1", [][5]string{{"x" + d, "S3", d, "USD", "7.00"}})
			if _, err := svc.Import(ctx, an, f.org1, "f.csv", strings.NewReader(csv)); err != nil {
				t.Errorf("import %d: %v", i, err)
			}
		}
	}()
	wg.Wait()

	// Final rebuild to converge, then verify.
	if _, err := svc.Rebuild(ctx); err != nil {
		t.Fatal(err)
	}

	var aprTotal string
	err := td.pool.QueryRow(ctx, `SELECT total::text FROM monthly_cost_center_summary
		WHERE cost_center_id=$1 AND period='2026-04-01' AND currency='USD'`, f.cc1).Scan(&aprTotal)
	if err != nil {
		t.Fatal(err)
	}
	if aprTotal != "70.000000" {
		t.Fatalf("april total = %s want 70 (10 x 7, no loss, no double count)", aprTotal)
	}

	// Raw row count invariant.
	var raw, dailyRows, monthlyRows int
	td.pool.QueryRow(ctx, `SELECT count(*) FROM costs c
		JOIN accounts a ON a.id=c.account_id WHERE a.cost_center_id=$1`, f.cc1).Scan(&raw)
	td.pool.QueryRow(ctx, `SELECT coalesce(sum(row_count),0) FROM daily_cost_center_summary
		WHERE cost_center_id=$1 AND currency='USD'`, f.cc1).Scan(&dailyRows)
	td.pool.QueryRow(ctx, `SELECT coalesce(sum(row_count),0) FROM monthly_cost_center_summary
		WHERE cost_center_id=$1 AND currency='USD'`, f.cc1).Scan(&monthlyRows)
	if dailyRows != raw || monthlyRows != raw {
		t.Fatalf("row counts raw=%d daily=%d monthly=%d", raw, dailyRows, monthlyRows)
	}
}

// TestCurrencyIsolation imports two currencies and asserts no summary ever
// mixes them.
func TestCurrencyIsolation(t *testing.T) {
	ctx := context.Background()
	td := newTestDB(t)
	f := newFixture(ctx, t, td.pool)
	svc := service.New(td.pool)
	an := principalFor(ctx, t, td.pool, f.analyst1Key)

	importCSV(ctx, t, svc, an, f.org1, billsFor("a1", [][5]string{
		{"r1", "EC2", "2026-05-01", "USD", "100.00"},
		{"r2", "S3", "2026-05-01", "EUR", "50.00"},
	}))

	type tot struct {
		cur, total string
	}
	var got []tot
	rows, err := td.pool.Query(ctx, `SELECT currency, total::text FROM monthly_cost_center_summary
		WHERE cost_center_id=$1 AND period='2026-05-01' ORDER BY currency`, f.cc1)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	for rows.Next() {
		var v tot
		if err := rows.Scan(&v.cur, &v.total); err != nil {
			t.Fatal(err)
		}
		got = append(got, v)
	}
	if len(got) != 2 || got[0].cur != "EUR" || got[0].total != "50.000000" ||
		got[1].cur != "USD" || got[1].total != "100.000000" {
		t.Fatalf("mixed currencies: %+v", got)
	}
}
