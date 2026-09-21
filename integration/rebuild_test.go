package integration

import (
	"fmt"
	"sync"
	"testing"
	"time"

	"desklens/internal/ingest"
	"desklens/internal/model"
	"desklens/internal/testsupport"

	"github.com/google/uuid"
)

// TestRebuildMatchesIncremental verifies that deleting every summary and
// rebuilding from raw rows produces exactly what incremental ingest produced.
func TestRebuildMatchesIncremental(t *testing.T) {
	e := setup(t)
	e.publishWidePolicy("00:00", "24:00")

	// Multiple employees/time zones across two departments and two weeks.
	e.mustIngest(p101, "rb-1",
		snapAt("WS-101", 101, utc(2026, 9, 15, 14, 0), "Chrome", 10),
		snapAt("WS-101", 101, utc(2026, 9, 15, 14, 1), "WeChat", 3),
		snapAt("WS-101", 101, utc(2026, 9, 16, 14, 0), "Code", 7))
	e.mustIngest(p102, "rb-2",
		// 2026-09-14 01:00 Shanghai (Monday).
		snapAt("WS-102", 102, utc(2026, 9, 13, 17, 0), "Chrome", 9))
	e.mustIngest(p103, "rb-3",
		// 2026-09-08 14:00 UTC = Tuesday 16:00 Berlin, prior week (Sep 7).
		snapAt("WS-103", 103, utc(2026, 9, 8, 14, 0), "Chrome", 4))

	type dailyKey struct {
		emp  int64
		date string
	}
	dailyBefore := map[dailyKey]int64{}
	weeklyBefore := map[[2]any]int64{}

	var dRows []struct {
		EmployeeID int64  `db:"employee_id"`
		LocalDate  string `db:"local_date"`
		Total      int64  `db:"total"`
	}
	if err := e.DB.Select(&dRows,
		`select employee_id, local_date::text as local_date, total_count as total
		 from daily_employee_summary`); err != nil {
		t.Fatal(err)
	}
	for _, r := range dRows {
		dailyBefore[dailyKey{r.EmployeeID, r.LocalDate}] = r.Total
	}
	var wRows []struct {
		Dept int64  `db:"dept"`
		Week string `db:"week"`
		Tot  int64  `db:"tot"`
	}
	if err := e.DB.Select(&wRows,
		`select department_id as dept, week_start::text as week, total_count as tot
		 from weekly_department_summary`); err != nil {
		t.Fatal(err)
	}
	for _, r := range wRows {
		weeklyBefore[[2]any{r.Dept, r.Week}] = r.Tot
	}
	if len(dailyBefore) < 4 || len(weeklyBefore) < 2 {
		t.Fatalf("fixture summaries too small: %d daily, %d weekly",
			len(dailyBefore), len(weeklyBefore))
	}

	// Wipe all derived data and rebuild from scratch.
	testsupport.MustExec(t, e.DB, `delete from daily_employee_summary`)
	testsupport.MustExec(t, e.DB, `delete from weekly_department_summary`)

	e.mustRebuild("2026-09-01", "2026-09-30")

	if err := e.DB.Select(&dRows,
		`select employee_id, local_date::text as local_date, total_count as total
		 from daily_employee_summary`); err != nil {
		t.Fatal(err)
	}
	gotDaily := map[dailyKey]int64{}
	for _, r := range dRows {
		gotDaily[dailyKey{r.EmployeeID, r.LocalDate}] = r.Total
	}
	if len(gotDaily) != len(dailyBefore) {
		t.Fatalf("rebuilt daily rows = %d, want %d", len(gotDaily), len(dailyBefore))
	}
	for k, want := range dailyBefore {
		if gotDaily[k] != want {
			t.Errorf("daily %+v rebuilt = %d, want %d", k, gotDaily[k], want)
		}
	}

	if err := e.DB.Select(&wRows,
		`select department_id as dept, week_start::text as week, total_count as tot
		 from weekly_department_summary`); err != nil {
		t.Fatal(err)
	}
	gotWeekly := map[[2]any]int64{}
	for _, r := range wRows {
		gotWeekly[[2]any{r.Dept, r.Week}] = r.Tot
	}
	for k, want := range weeklyBefore {
		if gotWeekly[k] != want {
			t.Errorf("weekly %+v rebuilt = %d, want %d", k, gotWeekly[k], want)
		}
	}
}

// TestLateArrivalRecomputeOnlyAffectedDate re-runs rebuild after an additional
// late row and confirms only the affected date/week change.
func TestRebuildLateDate(t *testing.T) {
	e := setup(t)
	e.publishWidePolicy("00:00", "24:00")

	e.mustIngest(p101, "rl-1",
		snapAt("WS-101", 101, utc(2026, 9, 15, 14, 0), "Chrome", 10))
	e.mustIngest(p101, "rl-2",
		snapAt("WS-101", 101, utc(2026, 9, 16, 14, 0), "Chrome", 20))

	// Late row for Sep 15 arrives later; rebuild only Sep 15.
	e.mustIngest(p101, "rl-3",
		snapAt("WS-101", 101, utc(2026, 9, 15, 13, 0), "Chrome", 100))
	e.mustRebuild("2026-09-15", "2026-09-15")

	d15, _ := testsupport.GetDaily(t, e.DB, 101, "2026-09-15")
	d16, _ := testsupport.GetDaily(t, e.DB, 101, "2026-09-16")
	if d15.TotalCount != 110 {
		t.Fatalf("Sep15 = %d, want 110", d15.TotalCount)
	}
	if d16.TotalCount != 20 {
		t.Fatalf("Sep16 = %d, want 20 (unaffected)", d16.TotalCount)
	}
}

// TestRebuildConcurrency ingests dozens of minutes while repeatedly rebuilding
// the same day. Final totals must equal the exact input sum: no lost writes and
// no double counting, regardless of interleaving.
func TestRebuildConcurrency(t *testing.T) {
	e := setup(t) // seeded v1 window covers 10:00-10:59 local NY

	const n = 60
	var wg sync.WaitGroup
	errCh := make(chan error, n*2)

	// Ingesters: disjoint minutes 14:00..14:59 UTC on Sep 15.
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			min := utc(2026, 9, 15, 14, 0).Add(time.Duration(i) * time.Minute)
			bid := uuid.NewSHA1(uuid.NameSpaceURL, []byte(fmt.Sprintf("desklens-test:cc-%03d", i))).String()
			_, err := e.Ingest.Ingest(background(), p101, ingest.Request{
				BatchID: &bid,
				Snapshots: []model.Snapshot{
					snapAt("WS-101", 101, min, "Chrome", i+1),
				},
			})
			if err != nil {
				errCh <- fmt.Errorf("ingest %d: %w", i, err)
			}
		}(i)
	}

	// Concurrent rebuilders hammering the same daily/weekly buckets.
	for r := 0; r < 8; r++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if _, err := e.Admin.Rebuild(background(), adminRebuildInput()); err != nil {
				errCh <- fmt.Errorf("rebuild: %w", err)
			}
		}()
	}

	wg.Wait()
	close(errCh)
	for err := range errCh {
		t.Fatal(err)
	}

	var want int64 = n * (n + 1) / 2 // 1+2+...+n
	d, ok := testsupport.GetDaily(t, e.DB, 101, "2026-09-15")
	if !ok {
		t.Fatal("daily row missing")
	}
	if d.TotalCount != want {
		t.Fatalf("daily total = %d, want %d", d.TotalCount, want)
	}
	w, ok := testsupport.GetWeekly(t, e.DB, 1, "2026-09-14")
	if !ok {
		t.Fatal("weekly row missing")
	}
	if w.TotalCount != want {
		t.Fatalf("weekly total = %d, want %d", w.TotalCount, want)
	}
	if cnt := testsupport.RawCount(t, e.DB, "WS-101"); cnt != n {
		t.Fatalf("raw rows = %d, want %d", cnt, n)
	}
}
