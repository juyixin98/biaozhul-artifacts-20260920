package service_test

import (
	"sync"
	"testing"
	"time"

	"costlens/internal/csvparse"
)

// snapshot holds the complete derived state so incremental and full rebuild
// can be compared byte-for-byte at the value level.
type snapshot struct {
	daily   []string
	monthly []string
	anomaly []string
}

func takeSnapshot(t *testing.T) snapshot {
	t.Helper()
	s := snapshot{}
	rows, err := pool.Query(ctx, `
		SELECT scope, COALESCE(account_id::text,'-'),
		       COALESCE(cost_center_id::text,'-'), usage_date::text,
		       currency::text, total_amount::text, record_count::text
		FROM daily_summaries
		ORDER BY 1,2,3,4,5`)
	if err != nil {
		t.Fatal(err)
	}
	for rows.Next() {
		var a, b, c, d, e, f, g string
		rows.Scan(&a, &b, &c, &d, &e, &f, &g)
		s.daily = append(s.daily, a+"|"+b+"|"+c+"|"+d+"|"+e+"|"+f+"|"+g)
	}
	rows.Close()
	rows, err = pool.Query(ctx, `
		SELECT scope, COALESCE(account_id::text,'-'),
		       COALESCE(cost_center_id::text,'-'), month::text,
		       currency::text, total_amount::text, record_count::text
		FROM monthly_summaries
		ORDER BY 1,2,3,4,5`)
	if err != nil {
		t.Fatal(err)
	}
	for rows.Next() {
		var a, b, c, d, e, f, g string
		rows.Scan(&a, &b, &c, &d, &e, &f, &g)
		s.monthly = append(s.monthly, a+"|"+b+"|"+c+"|"+d+"|"+e+"|"+f+"|"+g)
	}
	rows.Close()
	// Latest version per key only — rebuild adds versions but the visible
	// state for old keys must stay consistent.
	rows, err = pool.Query(ctx, `
		SELECT account_id::text, usage_date::text, currency::text, status,
		       actual_amount::text, COALESCE(baseline_mean::text,'-'),
		       COALESCE(baseline_std::text,'-'),
		       COALESCE(threshold_amount::text,'-'), baseline_days::text
		FROM anomaly_evaluations e
		WHERE version = (SELECT max(version) FROM anomaly_evaluations e2
		                 WHERE e2.account_id=e.account_id
		                   AND e2.usage_date=e.usage_date
		                   AND e2.currency=e.currency)
		ORDER BY 1,2,3`)
	if err != nil {
		t.Fatal(err)
	}
	for rows.Next() {
		var v [9]string
		for i := range v {
			rows.Scan(&v[i])
		}
		s.anomaly = append(s.anomaly, join(v[:]))
	}
	rows.Close()
	return s
}

func join(v []string) string {
	out := ""
	for i, s := range v {
		if i > 0 {
			out += "|"
		}
		out += s
	}
	return out
}

// ---------- Test 5: incremental == full rebuild ----------

func TestIncrementalEqualsRebuild(t *testing.T) {
	resetDB(t)
	f := newFixture(t, "rebuild-eq")
	today := dbToday(t)

	// Three staggered imports across the history window.
	mk := func(from, to time.Time, resource, amount string) []csvparse.Row {
		var rs []csvparse.Row
		for d := from; !d.After(to); d = d.AddDate(0, 0, 1) {
			rs = append(rs, row(resource, "compute", d, "USD", amount))
		}
		return rs
	}
	importRows(t, f.account,
		mk(today.AddDate(0, 0, -80), today.AddDate(0, 0, -60), "r1", "10"))
	importRows(t, f.account,
		mk(today.AddDate(0, 0, -55), today.AddDate(0, 0, -20), "r1", "10"))
	importRows(t, f.account,
		mk(today.AddDate(0, 0, -35), today.AddDate(0, 0, -1), "r1", "10"))

	incremental := takeSnapshot(t)

	if _, err := svc.Rebuild(ctx, 0); err != nil {
		t.Fatal(err)
	}
	rebuilt := takeSnapshot(t)

	if !equalStringSets(incremental.daily, rebuilt.daily) {
		t.Fatalf("daily mismatch\nincr=%v\nrebuild=%v", incremental.daily, rebuilt.daily)
	}
	if !equalStringSets(incremental.monthly, rebuilt.monthly) {
		t.Fatalf("monthly mismatch\nincr=%v\nrebuild=%v", incremental.monthly, rebuilt.monthly)
	}
	if !equalStringSets(incremental.anomaly, rebuilt.anomaly) {
		t.Fatalf("anomaly mismatch\nincr=%v\nrebuild=%v", incremental.anomaly, rebuilt.anomaly)
	}
}

func equalStringSets(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	m := map[string]int{}
	for _, s := range a {
		m[s]++
	}
	for _, s := range b {
		m[s]--
		if m[s] < 0 {
			return false
		}
	}
	return true
}

// ---------- Test 6: rebuild racing a concurrent import ----------

func TestRebuildRaceNoLossNoDoubleCount(t *testing.T) {
	resetDB(t)
	f := newFixture(t, "rebuild-race")
	today := dbToday(t)

	// Seed 40 days of 10 USD.
	var base []csvparse.Row
	for i := 45; i >= 1; i-- {
		base = append(base, row("r1", "compute", today.AddDate(0, 0, -i), "USD", "10"))
	}
	importRows(t, f.account, base)

	// Extra batch importing while rebuild runs.
	extraDay := today.AddDate(0, 0, -10)
	extra := []csvparse.Row{row("r-new", "compute", extraDay, "USD", "25")}

	var wg sync.WaitGroup
	wg.Add(2)
	var importErr error
	go func() { defer wg.Done(); _, importErr = svc.Import(ctx, f.account, "extra.csv", 0, extra) }()
	go func() {
		defer wg.Done()
		// Slight nudge so both contend on the lock; order either way is valid.
		time.Sleep(5 * time.Millisecond)
		_, _ = svc.Rebuild(ctx, 0)
	}()
	wg.Wait()
	if importErr != nil {
		t.Fatalf("import during rebuild: %v", importErr)
	}

	// The new row exists exactly once.
	if got := countBilling(t, f.account); got != 46 {
		t.Fatalf("want 46 records (45+1), got %d", got)
	}
	var total string
	pool.QueryRow(ctx, `
		SELECT total_amount::text FROM daily_summaries
		WHERE scope='account' AND usage_date=$1 AND currency='USD'`,
		extraDay).Scan(&total)
	if total != "35.000000" {
		t.Fatalf("extra day must total 35 after rebuild race, got %q", total)
	}

	// Another rebuild converges to the same values (idempotence).
	before := takeSnapshot(t)
	if _, err := svc.Rebuild(ctx, 0); err != nil {
		t.Fatal(err)
	}
	after := takeSnapshot(t)
	if !equalStringSets(before.daily, after.daily) ||
		!equalStringSets(before.monthly, after.monthly) {
		t.Fatal("rebuild must be idempotent")
	}
}
