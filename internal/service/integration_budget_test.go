package service_test

import (
	"testing"
	"time"

	"costlens/internal/csvparse"
	"costlens/internal/service"
)

// ---------- Test 7: budget thresholds fire once per version, history kept ----

func TestBudgetThresholdsAndVersioning(t *testing.T) {
	resetDB(t)
	f := newFixture(t, "budget")
	today := dbToday(t)
	monthS := today.Format("2006-01")

	// Budget 100 USD for the current month on this cost center.
	b1, err := svc.SetBudget(ctx, service.SetBudgetInput{
		CostCenterID: f.ccID, Currency: "USD",
		Month: monthS, MonthlyLimit: "100",
	}, 0)
	if err != nil {
		t.Fatal(err)
	}

	importAmount := func(resource string, amt string) {
		t.Helper()
		importRows(t, f.account, []csvparse.Row{
			row(resource, "compute", today, "USD", amt),
		})
	}

	// 60 -> 50% fires.
	importAmount("a1", "60")
	// 30 more (total 90) -> 75% and 90% fire at the first reach; 50% stays once.
	importAmount("a2", "30")
	// 20 more (total 110) -> 100% fires.
	importAmount("a3", "20")

	alerts := func(budgetID int64) map[int32]int {
		rows, err := pool.Query(ctx, `
			SELECT threshold_pct FROM budget_alerts WHERE budget_id=$1`, budgetID)
		if err != nil {
			t.Fatal(err)
		}
		m := map[int32]int{}
		for rows.Next() {
			var p int32
			rows.Scan(&p)
			m[p]++
		}
		rows.Close()
		return m
	}

	got := alerts(b1.ID)
	if got[50] != 1 || got[75] != 1 || got[90] != 1 || got[100] != 1 {
		t.Fatalf("each threshold must fire exactly once: %v", got)
	}

	// Spent amounts recorded with each alert reflect first-crossing totals.
	spent := map[int32]string{}
	rows, err := pool.Query(ctx, `
		SELECT threshold_pct, spent_amount::text FROM budget_alerts
		WHERE budget_id=$1 ORDER BY threshold_pct`, b1.ID)
	if err != nil {
		t.Fatal(err)
	}
	for rows.Next() {
		var p int32
		var s string
		rows.Scan(&p, &s)
		spent[p] = s
	}
	rows.Close()
	if spent[50] != "60.000000" || spent[75] != "90.000000" ||
		spent[90] != "90.000000" || spent[100] != "110.000000" {
		t.Fatalf("unexpected crossing totals: %v", spent)
	}

	// New budget version (raise limit); old alerts stay, fresh thresholds.
	b2, err := svc.SetBudget(ctx, service.SetBudgetInput{
		CostCenterID: f.ccID, Currency: "USD",
		Month: monthS, MonthlyLimit: "200",
	}, 0)
	if err != nil {
		t.Fatal(err)
	}
	if b2.Version != 2 {
		t.Fatalf("want version 2, got %d", b2.Version)
	}
	importAmount("a4", "10") // total 120 = 60% of 200 -> 50% on new version
	got2 := alerts(b2.ID)
	if got2[50] != 1 {
		t.Fatalf("new version must evaluate thresholds afresh: %v", got2)
	}
	// Historical alerts of v1 untouched.
	if len(alerts(b1.ID)) != 4 {
		t.Fatal("old version alerts must be preserved")
	}
	// Only v2 active.
	var activeCount int
	pool.QueryRow(ctx, `SELECT count(*) FROM budgets
		WHERE cost_center_id=$1 AND currency='USD' AND month=$2 AND active`,
		f.ccID, monthStart(today)).Scan(&activeCount)
	if activeCount != 1 {
		t.Fatalf("exactly one active budget version, got %d", activeCount)
	}
}

// ---------- Test 8: late data shifts baseline; old alert + new version kept --

func TestAnomalyLateDataBaselineVersions(t *testing.T) {
	resetDB(t)
	f := newFixture(t, "late")
	today := dbToday(t)

	// Target day D = yesterday, actual 12.
	// Batch A: 29 constant days (10 USD) at offsets 31..3, leaving exactly one
	// zero-spend hole in D's 30-day baseline (offset 2), plus the target row.
	target := today.AddDate(0, 0, -1)
	var batchA []csvparse.Row
	for i := 31; i >= 3; i-- {
		batchA = append(batchA,
			row("r1", "compute", today.AddDate(0, 0, -i), "USD", "10"))
	}
	batchA = append(batchA, row("r-target", "compute", target, "USD", "12"))
	importRows(t, f.account, batchA)

	statusAt := func(version int32) string {
		var s string
		err := pool.QueryRow(ctx, `
			SELECT status FROM anomaly_evaluations
			WHERE account_id=(SELECT id FROM accounts WHERE external_id=$1)
			  AND usage_date=$2 AND currency='USD' AND version=$3`,
			f.account, target, version).Scan(&s)
		if err != nil {
			t.Fatalf("v%d: %v", version, err)
		}
		return s
	}

	// Baseline = 29 tens + one zero: mean 9.6667, sd ~1.795, threshold ~13.26;
	// 12 is below it -> v1 normal.
	if s := statusAt(1); s != "normal" {
		t.Fatalf("v1 want normal, got %s", s)
	}

	// Late billing fills the hole at offset 2 (a different day than D).
	importRows(t, f.account, []csvparse.Row{
		row("late-r", "compute", today.AddDate(0, 0, -2), "USD", "10"),
	})

	// Baseline is now 30 constant tens: mean 10, std 0, threshold 10; 12 > 10.
	var nVersions int
	pool.QueryRow(ctx, `
		SELECT count(*) FROM anomaly_evaluations
		WHERE account_id=(SELECT id FROM accounts WHERE external_id=$1)
		  AND usage_date=$2 AND currency='USD'`,
		f.account, target).Scan(&nVersions)
	if nVersions != 2 {
		t.Fatalf("late data must append version 2 for D, got %d versions", nVersions)
	}
	if s := statusAt(2); s != "anomalous" {
		t.Fatalf("v2 want anomalous, got %s", s)
	}
	if s := statusAt(1); s != "normal" {
		t.Fatalf("v1 must be retained as normal, got %s", s)
	}
}

func monthStart(t time.Time) time.Time {
	return time.Date(t.Year(), t.Month(), 1, 0, 0, 0, 0, t.Location())
}

// A budget created after spend already crossed thresholds fires those
// thresholds immediately on the new version.
func TestBudgetCreatedAfterSpend(t *testing.T) {
	resetDB(t)
	f := newFixture(t, "latebudget")
	today := dbToday(t)
	importRows(t, f.account, []csvparse.Row{
		row("x1", "compute", today, "USD", "80"),
	})
	b, err := svc.SetBudget(ctx, service.SetBudgetInput{
		CostCenterID: f.ccID, Currency: "USD",
		Month: today.Format("2006-01"), MonthlyLimit: "100",
	}, 0)
	if err != nil {
		t.Fatal(err)
	}
	rows, err := pool.Query(ctx,
		`SELECT threshold_pct FROM budget_alerts WHERE budget_id=$1 ORDER BY 1`, b.ID)
	if err != nil {
		t.Fatal(err)
	}
	var got []int32
	for rows.Next() {
		var p int32
		rows.Scan(&p)
		got = append(got, p)
	}
	rows.Close()
	if len(got) != 2 || got[0] != 50 || got[1] != 75 {
		t.Fatalf("80%% spend must fire 50%% and 75%% at creation, got %v", got)
	}
}
