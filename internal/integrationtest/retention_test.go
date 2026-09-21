package integrationtest

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	"desklens/internal/aggregate"
	"desklens/internal/repo"
)

// After purging raw data, summaries survive, coverage is marked partial, and
// rebuild refuses to overwrite complete statistics with the incomplete
// remainder.
func TestPurgeRetainsSummariesAndBlocksRebuild(t *testing.T) {
	env := NewEnv(t)
	ctx := context.Background()

	if _, err := env.Ingest.Ingest(ctx, []repo.SnapshotIn{
		snap(1001, 101, "2026-03-10T10:00:00Z", "VS Code", 40),
		snap(1001, 101, "2026-03-10T10:01:00Z", "VS Code", 30),
		snap(1001, 101, "2026-03-12T10:00:00Z", "VS Code", 10),
	}); err != nil {
		t.Fatal(err)
	}

	cutoff := time.Date(2026, 3, 12, 0, 0, 0, 0, time.UTC)
	purged, err := env.Repo.PurgeRaw(ctx, cutoff)
	if err != nil {
		t.Fatal(err)
	}
	if purged != 2 {
		t.Fatalf("purged = %d, want 2", purged)
	}

	// Summaries retained.
	d := mustDaily(t, env, 101, "2026-03-10")
	if d.ProductiveCount != 70 {
		t.Fatalf("purged-day summary altered: %+v", d)
	}
	// Rebuildable window starts after the cutoff.
	bounds, err := env.Repo.RetentionBounds(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if bounds.OldestMinute == nil || !bounds.OldestMinute.Equal(time.Date(2026, 3, 12, 10, 0, 0, 0, time.UTC)) {
		t.Fatalf("bounds = %+v", bounds)
	}

	// Rebuilding the purged day/week is refused.
	if err := env.Agg.RebuildDaily(ctx, 101, date("2026-03-10")); !errors.Is(err, aggregate.ErrPartialCoverage) {
		t.Fatalf("day rebuild err = %v, want ErrPartialCoverage", err)
	}
	if err := env.Agg.RebuildWeekly(ctx, 1, date("2026-03-09")); !errors.Is(err, aggregate.ErrPartialCoverage) {
		t.Fatalf("week rebuild err = %v, want ErrPartialCoverage", err)
	}

	// Summary must remain the complete original value, not a partial sum.
	d = mustDaily(t, env, 101, "2026-03-10")
	if d.ProductiveCount != 70 {
		t.Fatalf("retained summary overwritten with partial data: %+v", d)
	}

	// Report classifies the purged keys as skipped but keeps the full day.
	report, err := env.Agg.RebuildAll(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if report.DaysSkipped < 1 || report.WeeksSkipped < 1 || report.DaysProcessed < 1 {
		t.Fatalf("report = %+v", report)
	}

	// Coverage row records partial state.
	var state string
	var minutes int
	if err := env.DB.QueryRow(
		`SELECT raw_state, raw_minutes FROM employee_day_coverage
		  WHERE employee_id = 101 AND local_date = DATE '2026-03-10'`).
		Scan(&state, &minutes); err != nil {
		t.Fatal(err)
	}
	if state != "partial" || minutes != 0 {
		t.Fatalf("coverage = %s/%d, want partial/0", state, minutes)
	}
}

// Rebuild concurrency: a full rebuild running concurrently with fresh
// ingestion into the same day must neither lose nor double-count the new
// rows. Final daily productive total equals the exact sum of all inputs.
func TestRebuildConcurrentWithIngestion(t *testing.T) {
	env := NewEnv(t)
	ctx := context.Background()
	day := date("2026-03-10")

	// Seed the day.
	seed := make([]repo.SnapshotIn, 0, 40)
	for m := 0; m < 40; m++ {
		seed = append(seed, snap(1001, 101,
			time.Date(2026, 3, 10, 10, m, 0, 0, time.UTC).Format(time.RFC3339),
			"VS Code", 1))
	}
	if _, err := env.Ingest.Ingest(ctx, seed); err != nil {
		t.Fatal(err)
	}

	var wg sync.WaitGroup
	wg.Add(2)
	var ingestErr error
	go func() { // Hammer new minutes into the same day/week.
		defer wg.Done()
		for m := 40; m < 120; m++ {
			// m maps to 10:40..11:59 — same local day and week.
			inst := time.Date(2026, 3, 10, 10+m/60, m%60, 0, 0, time.UTC)
			if _, err := env.Ingest.Ingest(ctx, []repo.SnapshotIn{
				snap(1001, 101, inst.Format(time.RFC3339), "VS Code", 1),
			}); err != nil {
				ingestErr = err
				return
			}
		}
	}()
	go func() { // Continuously rebuild the same day and its week.
		defer wg.Done()
		for i := 0; i < 30; i++ {
			if err := env.Agg.RebuildDaily(ctx, 101, day); err != nil {
				t.Errorf("rebuild day: %v", err)
				return
			}
			if err := env.Agg.RebuildWeekly(ctx, 1, date("2026-03-09")); err != nil {
				t.Errorf("rebuild week: %v", err)
				return
			}
			time.Sleep(time.Millisecond)
		}
	}()
	wg.Wait()
	if ingestErr != nil {
		t.Fatalf("ingestion: %v", ingestErr)
	}

	var n int
	if err := env.DB.Get(&n,
		`SELECT COUNT(*) FROM activity_snapshots WHERE employee_id = 101 AND local_date = $1`,
		day); err != nil {
		t.Fatal(err)
	}
	if n != 120 {
		t.Fatalf("raw rows = %d, want 120 (40 seed + 80 concurrent)", n)
	}

	// Final authoritative rebuild and check exact totals against raw rows.
	if err := env.Agg.RebuildDaily(ctx, 101, day); err != nil {
		t.Fatal(err)
	}
	var rawSum int64
	if err := env.DB.Get(&rawSum,
		`SELECT COALESCE(SUM(activity_count),0) FROM activity_snapshots
		  WHERE employee_id = 101 AND local_date = $1`, day); err != nil {
		t.Fatal(err)
	}
	var rawMinutes int
	if err := env.DB.Get(&rawMinutes,
		`SELECT COUNT(DISTINCT minute_utc) FROM activity_snapshots
		  WHERE employee_id = 101 AND local_date = $1`, day); err != nil {
		t.Fatal(err)
	}
	d := mustDaily(t, env, 101, "2026-03-10")
	if d.ProductiveCount != rawSum {
		t.Fatalf("daily productive %d != raw sum %d (lost/double counted rows)",
			d.ProductiveCount, rawSum)
	}
	if d.ActiveMinutes != rawMinutes {
		t.Fatalf("active minutes %d != raw minutes %d", d.ActiveMinutes, rawMinutes)
	}
}
