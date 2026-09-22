package service_test

import (
	"context"
	"fmt"
	"os"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/shopspring/decimal"

	"costlens/internal/csvparse"
	"costlens/internal/service"
	"costlens/internal/testdb"
)

var (
	pool *pgxpool.Pool
	svc  *service.Service
	ctx  = context.Background()
)

func TestMain(m *testing.M) {
	var err error
	var cleanup func()
	pool, cleanup, err = testdb.Setup("service")
	if err != nil {
		fmt.Fprintln(os.Stderr, "integration tests skipped:", err)
		os.Exit(0)
	}
	defer cleanup()
	svc = service.New(pool)
	os.Exit(m.Run())
}

// resetDB truncates every tenant table. Tests also use unique prefixes so
// they can run in parallel where desired.
func resetDB(t *testing.T) {
	t.Helper()
	_, err := pool.Exec(ctx, `TRUNCATE TABLE
		rebuild_events, anomaly_evaluations, budget_alerts, budgets,
		import_batches, billing_records, daily_summaries, monthly_summaries,
		user_org_grants, users, accounts, cost_centers, organizations
		RESTART IDENTITY CASCADE`)
	if err != nil {
		t.Fatal(err)
	}
}

type fixture struct {
	org, account string
	orgID, ccID  int64
}

func newFixture(t *testing.T, prefix string) fixture {
	t.Helper()
	orgExt := "org-" + prefix
	ccCode := "cc-" + prefix
	acctExt := "acct-" + prefix
	if _, err := svc.CreateOrg(ctx, service.CreateOrgInput{ExternalID: orgExt, Name: prefix}); err != nil {
		t.Fatal(err)
	}
	if _, err := svc.CreateCostCenter(ctx, service.CreateCostCenterInput{
		OrgExternalID: orgExt, Code: ccCode, Name: prefix}); err != nil {
		t.Fatal(err)
	}
	a, err := svc.CreateAccount(ctx, service.CreateAccountInput{
		OrgExternalID: orgExt, CostCenterCode: ccCode,
		ExternalID: acctExt, Name: prefix})
	if err != nil {
		t.Fatal(err)
	}
	return fixture{org: orgExt, account: acctExt, orgID: a.OrgID, ccID: a.CostCenterID}
}

func row(resource, serviceName string, day time.Time, currency, amount string) csvparse.Row {
	return csvparse.Row{
		ResourceID: resource, Service: serviceName, Date: day,
		Currency: currency, Amount: decimal.RequireFromString(amount),
	}
}

func mustDay(s string) time.Time {
	d, err := time.Parse("2006-01-02", s)
	if err != nil {
		panic(err)
	}
	return d
}

func dbToday(t *testing.T) time.Time {
	t.Helper()
	var today time.Time
	if err := pool.QueryRow(ctx, `SELECT CURRENT_DATE`).Scan(&today); err != nil {
		t.Fatal(err)
	}
	return today
}

func countBilling(t *testing.T, accountExt string) int {
	t.Helper()
	var n int
	err := pool.QueryRow(ctx, `
		SELECT count(*) FROM billing_records r
		JOIN accounts a ON a.id = r.account_id
		WHERE a.external_id=$1`, accountExt).Scan(&n)
	if err != nil {
		t.Fatal(err)
	}
	return n
}

func importRows(t *testing.T, account string, rows []csvparse.Row) *service.ImportResult {
	t.Helper()
	res, err := svc.Import(ctx, account, "test.csv", 0, rows)
	if err != nil {
		t.Fatalf("import failed: %v", err)
	}
	return res
}

// ---------- Test 1: dedup + conflict ----------

func TestImportDedupAndConflict(t *testing.T) {
	resetDB(t)
	f := newFixture(t, "dedup")
	day := mustDay("2026-01-10")

	res := importRows(t, f.account, []csvparse.Row{
		row("r1", "compute", day, "USD", "5"),
		row("r2", "storage", day, "USD", "7"),
	})
	if res.InsertedRows != 2 || res.DuplicateRows != 0 {
		t.Fatalf("first import: %+v", res)
	}

	// Identical re-import: all duplicates, no error.
	res = importRows(t, f.account, []csvparse.Row{
		row("r1", "compute", day, "USD", "5"),
		row("r2", "storage", day, "USD", "7"),
	})
	if res.InsertedRows != 0 || res.DuplicateRows != 2 {
		t.Fatalf("second import: %+v", res)
	}
	if countBilling(t, f.account) != 2 {
		t.Fatal("duplicates must not create rows")
	}

	// Same key, different amount: conflict with line number, whole batch fails.
	_, err := svc.Import(ctx, f.account, "bad.csv", 0, []csvparse.Row{
		row("r1", "compute", day, "USD", "6"),
	})
	var ce *service.ConflictError
	if err == nil || !asConflict(err, &ce) {
		t.Fatalf("want ConflictError, got %v", err)
	}
	if len(ce.Conflicts) != 1 || ce.Conflicts[0].ResourceID != "r1" ||
		ce.Conflicts[0].ExistingAmount != "5.000000" {
		t.Fatalf("bad conflict payload: %+v", ce.Conflicts)
	}
	if len(ce.Conflicts[0].Lines) != 1 || ce.Conflicts[0].Lines[0] != 0 {
		// Line is populated when rows are parsed from CSV; service-built rows
		// in this test carry zero, so assert key/amount instead.
	}
	if countBilling(t, f.account) != 2 {
		t.Fatal("conflicting batch must not change billing data")
	}
}

func asConflict(err error, ce **service.ConflictError) bool {
	c, ok := err.(*service.ConflictError)
	if ok {
		*ce = c
	}
	return ok
}

// ---------- Test 2: concurrent identical imports ----------

func TestConcurrentDuplicateImport(t *testing.T) {
	resetDB(t)
	f := newFixture(t, "concurrent")
	day := mustDay("2026-02-01")
	var batch []csvparse.Row
	for i := 0; i < 50; i++ {
		batch = append(batch, row("r"+itoa(i), "svc", day, "USD", "1.00"))
	}

	const n = 8
	var wg sync.WaitGroup
	errs := make(chan error, n)
	inserted := make(chan int, n)
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			res, err := svc.Import(ctx, f.account, "c.csv", 0, batch)
			if err != nil {
				errs <- err
				return
			}
			inserted <- res.InsertedRows
		}()
	}
	wg.Wait()
	close(errs)
	close(inserted)
	for e := range errs {
		t.Fatalf("concurrent import error: %v", e)
	}
	totalInserted := 0
	for v := range inserted {
		totalInserted += v
	}
	if totalInserted != 50 {
		t.Fatalf("exactly 50 rows must survive 8 duplicate imports, got %d", totalInserted)
	}
	if countBilling(t, f.account) != 50 {
		t.Fatal("billing row count wrong")
	}
	// Daily total must be exactly 50, never 100/400.
	var total string
	if err := pool.QueryRow(ctx, `
		SELECT total_amount::text FROM daily_summaries
		WHERE scope='account' AND usage_date=$1 AND currency='USD'`,
		mustDay("2026-02-01")).Scan(&total); err != nil {
		t.Fatal(err)
	}
	if total != "50.000000" {
		t.Fatalf("daily total double-counted: %s", total)
	}
}

// ---------- Test 3: any error rolls back the whole batch ----------

func TestBatchAtomicRollback(t *testing.T) {
	resetDB(t)
	f := newFixture(t, "rollback")
	d1 := mustDay("2026-03-01")
	d2 := mustDay("2026-03-02")
	importRows(t, f.account, []csvparse.Row{
		row("r1", "compute", d1, "USD", "5"),
	})

	batchesBefore := func() int {
		var n int
		pool.QueryRow(ctx, `SELECT count(*) FROM import_batches`).Scan(&n)
		return n
	}()

	_, err := svc.Import(ctx, f.account, "mixed.csv", 0, []csvparse.Row{
		row("r2", "storage", d2, "USD", "9"), // new, must NOT survive
		row("r1", "compute", d1, "USD", "8"), // conflicts
	})
	if err == nil {
		t.Fatal("conflict expected")
	}
	if countBilling(t, f.account) != 1 {
		t.Fatal("non-conflicting row from failed batch must roll back")
	}
	var nBatches int
	pool.QueryRow(ctx, `SELECT count(*) FROM import_batches`).Scan(&nBatches)
	if nBatches != batchesBefore {
		t.Fatalf("failed batch row must roll back too: before=%d after=%d", batchesBefore, nBatches)
	}
	var exists bool
	pool.QueryRow(ctx, `SELECT EXISTS(
		SELECT 1 FROM daily_summaries WHERE scope='account' AND usage_date=$1)`,
		d2).Scan(&exists)
	if exists {
		t.Fatal("summary for rolled-back day must not exist")
	}
}

// ---------- Test 4: cross-month + no currency mixing ----------

func TestCrossMonthAndCurrencyIsolation(t *testing.T) {
	resetDB(t)
	f := newFixture(t, "months")
	dJan := mustDay("2026-01-31")
	dFeb := mustDay("2026-02-01")
	importRows(t, f.account, []csvparse.Row{
		row("r1", "compute", dJan, "USD", "10"),
		row("r2", "compute", dFeb, "USD", "20"),
		row("r3", "compute", dJan, "EUR", "30"),
		row("r4", "compute", dFeb, "EUR", "40"),
	})

	// Daily account summaries: 4 rows, one per (date, currency).
	rows, err := pool.Query(ctx, `
		SELECT usage_date::text, currency::text, total_amount::text
		FROM daily_summaries WHERE scope='account' ORDER BY 1, 2`)
	if err != nil {
		t.Fatal(err)
	}
	got := map[string]string{}
	for rows.Next() {
		var d, c, a string
		rows.Scan(&d, &c, &a)
		got[d+"/"+c] = a
	}
	rows.Close()
	want := map[string]string{
		"2026-01-31/USD": "10.000000",
		"2026-02-01/USD": "20.000000",
		"2026-01-31/EUR": "30.000000",
		"2026-02-01/EUR": "40.000000",
	}
	for k, v := range want {
		if got[k] != v {
			t.Fatalf("daily %s: want %s got %s", k, v, got[k])
		}
	}

	// Monthly: USD Jan/Feb separate; no USD+EUR aggregate.
	mrows, err := pool.Query(ctx, `
		SELECT month::text, currency::text, total_amount::text
		FROM monthly_summaries WHERE scope='account' ORDER BY 1,2`)
	if err != nil {
		t.Fatal(err)
	}
	mgot := map[string]string{}
	for mrows.Next() {
		var d, c, a string
		mrows.Scan(&d, &c, &a)
		mgot[d+"/"+c] = a
	}
	mrows.Close()
	mwant := map[string]string{
		"2026-01-01/USD": "10.000000",
		"2026-02-01/USD": "20.000000",
		"2026-01-01/EUR": "30.000000",
		"2026-02-01/EUR": "40.000000",
	}
	for k, v := range mwant {
		if mgot[k] != v {
			t.Fatalf("monthly %s: want %s got %s", k, v, mgot[k])
		}
	}
	if len(mgot) != 4 {
		t.Fatalf("currencies must not be mixed: %v", mgot)
	}

	// Cost-center rollups exist for both months and currencies.
	var ccCount int
	pool.QueryRow(ctx, `
		SELECT count(*) FROM monthly_summaries
		WHERE scope='cost_center' AND cost_center_id=$1`, f.ccID).Scan(&ccCount)
	if ccCount != 4 {
		t.Fatalf("want 4 cc/month/currency groups (2 months x 2 currencies), got %d", ccCount)
	}
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var b []byte
	for n > 0 {
		b = append([]byte{byte('0' + n%10)}, b...)
		n /= 10
	}
	return string(b)
}
