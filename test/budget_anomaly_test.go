package integration

import (
	"context"
	"testing"
	"time"

	"costlens/internal/service"
	"github.com/shopspring/decimal"
)

// TestBudgetThresholdDedup: alerts fire once at 50/75/90/100 first crossing;
// repeated imports below the next threshold add nothing; a budget revision
// creates a fresh independent alert set while old alerts remain.
func TestBudgetThresholdDedup(t *testing.T) {
	ctx := context.Background()
	td := newTestDB(t)
	f := newFixture(ctx, t, td.pool)
	svc := service.New(td.pool)
	an := principalFor(ctx, t, td.pool, f.analyst1Key)

	period := "2026-06-01"
	createBudget := func(amount string) string {
		b, err := svc.CreateBudget(ctx, an, service.CreateBudgetInput{
			CostCenterID: f.cc1, Currency: "USD", Period: period,
			Amount: decimal.RequireFromString(amount),
		})
		if err != nil {
			t.Fatal(err)
		}
		return b.ID
	}
	v1 := createBudget("1000")

	alertCount := func() int {
		var n int
		err := td.pool.QueryRow(ctx,
			`SELECT count(*) FROM budget_alerts WHERE cost_center_id=$1 AND period=$2`,
			f.cc1, period).Scan(&n)
		if err != nil {
			t.Fatal(err)
		}
		return n
	}
	versionCounts := func() map[int]int {
		m := map[int]int{}
		rows, err := td.pool.Query(ctx, `SELECT bv.version, count(*)
			FROM budget_alerts ba JOIN budget_versions bv ON bv.id=ba.budget_version_id
			WHERE ba.cost_center_id=$1 AND ba.period=$2 GROUP BY bv.version`, f.cc1, period)
		if err != nil {
			t.Fatal(err)
		}
		defer rows.Close()
		for rows.Next() {
			var v, c int
			rows.Scan(&v, &c)
			m[v] = c
		}
		return m
	}

	// Spend 600 => crosses 50 (0.6 >= 0.5) but not 75.
	importCSV(ctx, t, svc, an, f.org1, billsFor("a1", [][5]string{
		{"r1", "EC2", "2026-06-01", "USD", "600.00"},
	}))
	if alertCount() != 1 {
		t.Fatalf("after 600: alerts=%d want 1", alertCount())
	}
	// Re-importing the same data must not duplicate.
	importCSV(ctx, t, svc, an, f.org1, billsFor("a1", [][5]string{
		{"r1", "EC2", "2026-06-01", "USD", "600.00"},
	}))
	if alertCount() != 1 {
		t.Fatalf("duplicate import: alerts=%d want 1", alertCount())
	}

	// 150 more => 750 crosses exactly 75.
	importCSV(ctx, t, svc, an, f.org1, billsFor("a1", [][5]string{
		{"r2", "S3", "2026-06-02", "USD", "150.00"},
	}))
	if c := alertCount(); c != 2 {
		t.Fatalf("after 750: alerts=%d want 2", c)
	}

	// 150 more => 900 crosses 90.
	importCSV(ctx, t, svc, an, f.org1, billsFor("a1", [][5]string{
		{"r3", "RDS", "2026-06-03", "USD", "150.00"},
	}))
	if c := alertCount(); c != 3 {
		t.Fatalf("after 900: alerts=%d want 3", c)
	}

	// 200 more => 1100 crosses 100.
	importCSV(ctx, t, svc, an, f.org1, billsFor("a1", [][5]string{
		{"r4", "LMB", "2026-06-04", "USD", "200.00"},
	}))
	if c := alertCount(); c != 4 {
		t.Fatalf("after 1100: alerts=%d want 4", c)
	}

	// Exactly-at-boundary semantics: a new resource at 0 (zero) changes nothing.
	importCSV(ctx, t, svc, an, f.org1, billsFor("a1", [][5]string{
		{"r5", "ZER", "2026-06-05", "USD", "0.00"},
	}))
	if c := alertCount(); c != 4 {
		t.Fatalf("zero spend: alerts=%d want 4", c)
	}

	// Budget revision v2 (lower amount): alerts evaluated against version 2
	// independently; v1's four alerts remain.
	v2 := createBudget("500")
	if v1 == v2 {
		t.Fatal("expected distinct budget version ids")
	}
	vc := versionCounts()
	if vc[1] != 4 {
		t.Fatalf("v1 alerts=%d want 4 (history retained)", vc[1])
	}
	if vc[2] != 4 {
		t.Fatalf("v2 alerts=%d want 4 (1100/500 crosses all thresholds)", vc[2])
	}

	// Exact threshold boundary: fresh budget 1100 => 1100/1100 = 1 crosses all.
	createBudget("1100")
	vc = versionCounts()
	if vc[3] != 4 {
		t.Fatalf("boundary v3 alerts=%d want 4", vc[3])
	}
}

// TestAnomalyBaselineEdges covers insufficient history (new account), zero
// variance strict-greater, and that late data creates a new evaluation version
// while old alerts are retained.
func TestAnomalyBaselineEdges(t *testing.T) {
	ctx := context.Background()
	td := newTestDB(t)
	f := newFixture(ctx, t, td.pool)
	svc := service.New(td.pool)
	an := principalFor(ctx, t, td.pool, f.analyst1Key)

	// Account a1 existed since 2025; give it a perfectly flat 30-day window.
	var rows [][5]string
	base := time.Date(2026, 7, 1, 0, 0, 0, 0, time.UTC)
	for i := 0; i < 30; i++ {
		d := base.AddDate(0, 0, i).Format("2006-01-02") // Jul1..Jul30
		rows = append(rows, [5]string{"r" + d, "EC2", d, "USD", "100.00"})
	}
	importCSV(ctx, t, svc, an, f.org1, billsFor("a1", rows))

	evalStatus := func(date string) (string, int) {
		var status string
		var n int
		err := td.pool.QueryRow(ctx, `SELECT status, count(*) FROM (
			SELECT status FROM anomaly_evaluations
			WHERE account_id=$1 AND currency='USD' AND cost_date=$2
			ORDER BY created_at DESC LIMIT 1) t GROUP BY status`, f.acct1, date).Scan(&status, &n)
		if err != nil {
			t.Fatal(err)
		}
		return status, n
	}

	// Jul31 with a flat baseline (mean 100, std 0): exactly 100 is normal...
	importCSV(ctx, t, svc, an, f.org1, billsFor("a1", [][5]string{
		{"r-0731", "EC2", "2026-07-31", "USD", "100.00"},
	}))
	if st, _ := evalStatus("2026-07-31"); st != "normal" {
		t.Fatalf("jul31 equal-to-mean: %s want normal (strict >)", st)
	}

	// ...100.01 is an anomaly under zero variance.
	importCSV(ctx, t, svc, an, f.org1, billsFor("a1", [][5]string{
		{"r-0801", "EC2", "2026-08-01", "USD", "100.01"},
	}))
	if st, _ := evalStatus("2026-08-01"); st != "anomaly" {
		t.Fatalf("aug1 above-mean: %s want anomaly", st)
	}

	// Insufficient history: account a2 (created 2025 too in fixture) — instead
	// create a brand new account on Jul 20 2026 and evaluate Jul 31.
	newAcct := f.newAccount(ctx, t, td.pool, "a-new", time.Date(2026, 7, 20, 0, 0, 0, 0, time.UTC))
	importCSV(ctx, t, svc, an, f.org1, billsFor("a-new", [][5]string{
		{"rn1", "EC2", "2026-07-31", "USD", "9999.00"},
	}))
	var status string
	err := td.pool.QueryRow(ctx, `SELECT status FROM anomaly_evaluations
		WHERE account_id=$1 AND cost_date='2026-07-31'
		ORDER BY created_at DESC LIMIT 1`, newAcct).Scan(&status)
	if err != nil {
		t.Fatal(err)
	}
	if status != "insufficient_history" {
		t.Fatalf("new account status=%s want insufficient_history", status)
	}

	// Late data changes the baseline for Jul31; old evaluation rows stay.
	before := countRows(ctx, t, td.pool,
		`SELECT count(*) FROM anomaly_evaluations WHERE account_id=$1 AND cost_date='2026-07-31'`, f.acct1)
	importCSV(ctx, t, svc, an, f.org1, billsFor("a1", [][5]string{
		{"r-late07", "S3", "2026-07-15", "USD", "5000.00"}, // previously empty day
	}))
	after := countRows(ctx, t, td.pool,
		`SELECT count(*) FROM anomaly_evaluations WHERE account_id=$1 AND cost_date='2026-07-31'`, f.acct1)
	if after <= before {
		t.Fatalf("expected a new evaluation version after late data: before=%d after=%d", before, after)
	}
}
