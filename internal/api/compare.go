package api

import (
	"sitevitals/internal/models"
	"sitevitals/internal/store"
)

// runSummaryView is the comparison view of one run with metrics keyed by name.
type runSummaryView struct {
	RunID   uint64                   `json:"run_id"`
	JobID   uint64                   `json:"job_id"`
	Status  string                   `json:"status"`
	Metrics map[string]models.Metric `json:"metrics"`
}

func buildRunSummary(a *store.RunArtifacts) runSummaryView {
	m := map[string]models.Metric{}
	for _, metric := range a.Metrics {
		m[metric.Name] = metric
	}
	return runSummaryView{RunID: a.Run.ID, JobID: a.Run.JobID, Status: a.Run.Status, Metrics: m}
}

// metricDelta is one metric's change between two successful runs.
type metricDelta struct {
	Metric      string   `json:"metric"`
	OldStatus   string   `json:"old_status"`
	NewStatus   string   `json:"new_status"`
	OldValueMS  *float64 `json:"old_value_ms,omitempty"`
	NewValueMS  *float64 `json:"new_value_ms,omitempty"`
	OldValueCLS *float64 `json:"old_value_cls,omitempty"`
	NewValueCLS *float64 `json:"new_value_cls,omitempty"`
	DeltaMS     *float64 `json:"delta_ms,omitempty"`
	DeltaCLS    *float64 `json:"delta_cls,omitempty"`
	Degraded    bool     `json:"degraded"`
	// Comparable is false when either side is missing/unsupported: no verdict.
	Comparable bool `json:"comparable"`
}

func metricDeltas(old, newer []models.Metric) []metricDelta {
	oldBy := map[string]models.Metric{}
	for _, m := range old {
		oldBy[m.Name] = m
	}
	out := []metricDelta{}
	seen := map[string]bool{}
	for _, m := range newer {
		seen[m.Name] = true
		out = append(out, oneDelta(oldBy[m.Name], m))
	}
	for _, m := range old {
		if !seen[m.Name] {
			missing := models.Metric{Name: m.Name, Status: "absent"}
			out = append(out, oneDelta(m, missing))
		}
	}
	return out
}

func oneDelta(o, n models.Metric) metricDelta {
	d := metricDelta{
		Metric: n.Name, OldStatus: o.Status, NewStatus: n.Status,
		OldValueMS: o.ValueMS, NewValueMS: n.ValueMS,
		OldValueCLS: o.ValueCLS, NewValueCLS: n.ValueCLS,
	}
	if o.Status == models.MetricCollected && n.Status == models.MetricCollected {
		d.Comparable = true
		switch n.Name {
		case "cls":
			if o.ValueCLS != nil && n.ValueCLS != nil {
				diff := *n.ValueCLS - *o.ValueCLS
				d.DeltaCLS = &diff
				d.Degraded = diff > 0
			}
		default:
			if o.ValueMS != nil && n.ValueMS != nil {
				diff := *n.ValueMS - *o.ValueMS
				d.DeltaMS = &diff
				d.Degraded = diff > 0
			}
		}
	}
	return d
}
