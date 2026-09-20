package budget

import (
	"sitevitals/internal/models"
)

// Metric names used in budget_alerts.metric.
const (
	MetricNavDuration   = "nav_duration_ms"
	MetricFCP           = "fcp_ms"
	MetricLCP           = "lcp_ms"
	MetricCLS           = "cls"
	MetricLongTaskTotal = "long_task_total_ms"
)

// Check performs the threshold comparison for a fully populated successful run.
func Check(taskID, runID uint, b *models.Budget, r *models.Run) []models.BudgetAlert {
	var alerts []models.BudgetAlert
	add := func(metric string, actual, threshold float64) {
		if actual > threshold {
			alerts = append(alerts, models.BudgetAlert{
				TaskID: taskID, RunID: runID,
				Metric: metric, Actual: actual, Threshold: threshold, Severity: "warn",
			})
		}
	}
	if b.NavDurationMS != nil && r.NavDurationMS != nil {
		add(MetricNavDuration, *r.NavDurationMS, *b.NavDurationMS)
	}
	if b.FCPMS != nil && r.FCPMS != nil {
		add(MetricFCP, *r.FCPMS, *b.FCPMS)
	}
	if b.LCPMS != nil && r.LCPMS != nil {
		add(MetricLCP, *r.LCPMS, *b.LCPMS)
	}
	if b.CLS != nil && r.CLS != nil {
		add(MetricCLS, *r.CLS, *b.CLS)
	}
	if b.LongTaskTotalMS != nil && r.LongTaskTotalMS != nil {
		add(MetricLongTaskTotal, *r.LongTaskTotalMS, *b.LongTaskTotalMS)
	}
	return alerts
}
