package report_test

import (
	"strings"
	"testing"
	"time"

	"sitevitals/internal/models"
	"sitevitals/internal/report"
)

func TestBuild_MissingMetricsShownAsNA_NotZero(t *testing.T) {
	task := &models.Task{ID: 42}
	run := &models.Run{
		ID: 1, TaskID: 42, AttemptNo: 1,
		Status: models.StateSucceeded, Viewport: models.ViewportMobile,
		URL:       "http://demo.local/x",
		FinalURL:  "http://demo.local/x",
		StartedAt: time.Now().Add(-time.Second),
		// Only FCP was measured; LCP/CLS/nav/long-tasks are absent.
		FCPMS: ptr(412.5),
		MetricStatus: models.MetricSet{
			Navigation: models.MetricUnsupported,
			FCP:        models.MetricOK,
			LCP:        models.MetricFailed,
			CLS:        models.MetricUnsupported,
			LongTasks:  models.MetricUnsupported,
			Resources:  models.MetricOK,
		},
	}
	alerts := []models.BudgetAlert{{Metric: "fcp_ms", Actual: 412.5, Threshold: 300, Severity: "warn"}}
	md := report.Build(task, run, alerts)

	for _, want := range []string{
		"412.5 ms",
		"N/A", // at least one missing metric
		"unsupported",
		"failed",
		"412.500", // actual value in alert row
		"300.000", // threshold in alert row
		"task 42",
	} {
		if !strings.Contains(md, want) {
			t.Errorf("report missing %q\n%s", want, md)
		}
	}
	if strings.Contains(md, "| 0.0 ms |") {
		t.Error("a missing metric was rendered as 0")
	}
}

func ptr(v float64) *float64 { return &v }
