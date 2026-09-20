package budget_test

import (
	"testing"

	"sitevitals/internal/budget"
	"sitevitals/internal/models"
)

func fp(v float64) *float64 { return &v }

func TestCheck_ThresholdsAndMissingMetrics(t *testing.T) {
	b := &models.Budget{
		FCPMS: fp(1800), LCPMS: fp(2500), CLS: fp(0.1),
		NavDurationMS: fp(3000), LongTaskTotalMS: fp(1000),
	}
	run := &models.Run{
		FCPMS:           fp(1900), // over -> alert
		LCPMS:           fp(2000), // under -> no alert
		CLS:             nil,      // missing (unsupported) -> must NOT alert
		NavDurationMS:   nil,
		LongTaskTotalMS: fp(1001), // over -> alert
	}
	alerts := budget.Check(7, 9, b, run)
	got := map[string]bool{}
	for _, a := range alerts {
		got[a.Metric] = true
		if a.RunID != 9 || a.TaskID != 7 {
			t.Fatalf("alert not linked to run/task: %+v", a)
		}
	}
	if !got[budget.MetricFCP] || !got[budget.MetricLongTaskTotal] {
		t.Fatalf("expected FCP + long-task alerts, got %v", got)
	}
	if got[budget.MetricLCP] || got[budget.MetricCLS] || got[budget.MetricNavDuration] {
		t.Fatalf("unexpected alerts for under/missing metrics: %v", got)
	}
}

func TestCheck_NilThresholdsSkipped(t *testing.T) {
	run := &models.Run{FCPMS: fp(999999)}
	if as := budget.Check(1, 1, &models.Budget{}, run); len(as) != 0 {
		t.Fatalf("no thresholds configured means no alerts, got %v", as)
	}
}
