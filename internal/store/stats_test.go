package store_test

import (
	"context"
	"testing"
	"time"

	"sitevitals/internal/models"
	"sitevitals/internal/store"
	"sitevitals/internal/testutil"
)

// TestStats_FailedRunsExcluded verifies the "failures never mix into success
// statistics" rule, including per-metric NULL exclusion.
func TestStats_FailedRunsExcluded(t *testing.T) {
	st := testutil.NewStore(t)
	ctx := context.Background()
	const u = "http://demo.local/stats"
	vp := models.ViewportDesktop

	// Successful run with FCP=100 and no LCP.
	t1, _ := st.Enqueue(ctx, store.EnqueueRequest{URL: u, Viewport: vp, MaxAttempts: 1})
	c1, _ := st.Claim(ctx, "w", time.Minute)
	fcp := 100.0
	m1 := &store.RunMetrics{
		FCPMS: &fcp,
		MetricStatus: models.MetricSet{
			Navigation: models.MetricOK, FCP: models.MetricOK,
			LCP: models.MetricFailed, CLS: models.MetricOK,
			LongTasks: models.MetricOK, Resources: models.MetricOK,
		},
	}
	if err := st.CommitSuccess(ctx, store.SuccessInput{
		TaskID: t1.ID, Token: *c1.Task.LeaseToken, RunID: c1.Run.ID,
		FinalURL: u, Collected: m1, ReportMD: "r1",
	}); err != nil {
		t.Fatal(err)
	}

	// Failed run must not count at all.
	t2, _ := st.Enqueue(ctx, store.EnqueueRequest{URL: u, Viewport: vp, MaxAttempts: 1})
	c2, _ := st.Claim(ctx, "w", time.Minute)
	if err := st.CommitFailure(ctx, store.FailureInput{
		TaskID: t2.ID, Token: *c2.Task.LeaseToken, RunID: c2.Run.ID,
		ErrorCode: "NAVIGATION_TIMEOUT", ErrorMsg: "x",
	}); err != nil {
		t.Fatal(err)
	}

	stats, err := st.Stats(ctx, u, vp)
	if err != nil {
		t.Fatal(err)
	}
	if stats.SuccessfulRuns != 1 {
		t.Fatalf("successful runs = %d, failed run leaked in", stats.SuccessfulRuns)
	}
	if stats.AvgFCPMS == nil || *stats.AvgFCPMS != 100 {
		t.Fatalf("avg FCP wrong: %v", stats.AvgFCPMS)
	}
	if stats.AvgLCPMS != nil {
		t.Fatalf("NULL LCP must be excluded, avg = %v", stats.AvgLCPMS)
	}
}

// TestBudgets_AlertsAndEffectiveMerge covers global default seeding and
// per-site override merging.
func TestBudgets_AlertsAndEffectiveMerge(t *testing.T) {
	st := testutil.NewStore(t)
	ctx := context.Background()

	global, err := st.EffectiveBudget(ctx, 0)
	if err != nil || global.FCPMS == nil {
		t.Fatalf("seeded defaults: %v %+v", err, global)
	}

	site, err := st.EnsureSite(ctx, "x", "http://x.local", "/", true)
	if err != nil {
		t.Fatal(err)
	}
	tight := 999.0
	if err := st.UpsertBudget(ctx, &models.Budget{SiteID: site.ID, FCPMS: &tight}); err != nil {
		t.Fatal(err)
	}
	eff, err := st.EffectiveBudget(ctx, site.ID)
	if err != nil {
		t.Fatal(err)
	}
	if eff.FCPMS == nil || *eff.FCPMS != 999 {
		t.Fatalf("site FCP override not merged: %v", eff.FCPMS)
	}
	if eff.LCPMS == nil || *eff.LCPMS != *global.LCPMS {
		t.Fatal("unset site threshold must fall back to global default")
	}
}
