package budget

import (
	"context"
	"testing"
	"time"

	"sitevitals/internal/collector"
	"sitevitals/internal/models"
	"sitevitals/internal/store"
)

func evalDB(t *testing.T) (*store.Repo, *gormWrap) {
	t.Helper()
	db := openTestDB(t)
	return store.NewRepo(db), &gormWrap{db: db}
}

func TestEvaluate(t *testing.T) {
	repo, w := evalDB(t)
	ctx := context.Background()
	const u = "http://x.test/p"

	// Two successful runs, both missing LCP.
	run1 := &models.Run{JobID: 1, Attempt: 1, Viewport: models.ViewportDesktop,
		TargetURL: u, Status: models.RunSucceeded, LeaseHolder: "h", FencingToken: 1,
		StartedAt: time.Now()}
	if err := w.db.Create(run1).Error; err != nil {
		t.Fatal(err)
	}
	fcp1, cls1 := 100.0, 0.05
	mustCreate(t, w, &models.Metric{RunID: run1.ID, Name: collector.MetricFCP, Status: models.MetricCollected, ValueMS: &fcp1})
	mustCreate(t, w, &models.Metric{RunID: run1.ID, Name: collector.MetricCLS, Status: models.MetricCollected, ValueCLS: &cls1})
	mustCreate(t, w, &models.Metric{RunID: run1.ID, Name: collector.MetricLCP, Status: models.MetricUnsupported})

	// Budgets: FCP under threshold, CLS over threshold, LCP no threshold value (skipped).
	fcpTh, clsTh, lcpTh := 200.0, 0.01, 500.0
	mustCreate(t, w, &models.Budget{TargetURL: u, Viewport: models.ViewportDesktop, Metric: collector.MetricFCP, ThresholdMS: &fcpTh, Enabled: true})
	mustCreate(t, w, &models.Budget{TargetURL: u, Viewport: models.ViewportDesktop, Metric: collector.MetricCLS, ThresholdCLS: &clsTh, Enabled: true})
	mustCreate(t, w, &models.Budget{TargetURL: u, Viewport: models.ViewportDesktop, Metric: collector.MetricLCP, ThresholdMS: &lcpTh, Enabled: true})

	ev := NewEvaluator(repo)
	evals, err := ev.Evaluate(ctx, run1)
	if err != nil {
		t.Fatal(err)
	}
	if len(evals) != 3 {
		t.Fatalf("evals=%d want 3", len(evals))
	}
	byMetric := map[string]models.BudgetEvaluation{}
	for _, e := range evals {
		byMetric[e.Metric] = e
	}
	if byMetric[collector.MetricFCP].Exceeded || byMetric[collector.MetricFCP].Skipped {
		t.Errorf("fcp should be within budget: %+v", byMetric[collector.MetricFCP])
	}
	if !byMetric[collector.MetricCLS].Exceeded || byMetric[collector.MetricCLS].Skipped {
		t.Errorf("cls should exceed budget: %+v", byMetric[collector.MetricCLS])
	}
	lcpEval := byMetric[collector.MetricLCP]
	if !lcpEval.Skipped || lcpEval.Exceeded {
		t.Errorf("missing LCP must be skipped, not zero-compared: %+v", lcpEval)
	}
	if lcpEval.ActualMS != nil {
		t.Errorf("skipped eval must not record actual value, got %v", *lcpEval.ActualMS)
	}
}

func mustCreate(t *testing.T, w *gormWrap, v any) {
	t.Helper()
	if err := w.db.Create(v).Error; err != nil {
		t.Fatalf("create: %v", err)
	}
}
