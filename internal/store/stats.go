package store

import (
	"context"

	"sitevitals/internal/models"
)

// RunStats aggregates successful runs only. Failed, dead and abandoned runs
// never enter these numbers — NULL metrics are also skipped per metric, so a
// run that failed to produce LCP cannot drag the LCP average to zero.
type RunStats struct {
	SuccessfulRuns   int64    `json:"successful_runs"`
	AvgNavDurationMS *float64 `json:"avg_nav_duration_ms"`
	AvgFCPMS         *float64 `json:"avg_fcp_ms"`
	AvgLCPMS         *float64 `json:"avg_lcp_ms"`
	AvgCLS           *float64 `json:"avg_cls"`
	AvgLongTaskTotal *float64 `json:"avg_long_task_total_ms"`
}

// TaskCounts summarizes queue states (all runs, including failures).
type TaskCounts struct {
	Queued    int64 `json:"queued"`
	Running   int64 `json:"running"`
	Succeeded int64 `json:"succeeded"`
	Failed    int64 `json:"failed"`
	Dead      int64 `json:"dead"`
}

func (s *Store) TaskCounts(ctx context.Context) (TaskCounts, error) {
	var c TaskCounts
	rows := []struct {
		Status string
		N      int64
	}{}
	if err := s.db.WithContext(ctx).Model(&models.Task{}).
		Select("status, count(*) as n").Group("status").Scan(&rows).Error; err != nil {
		return c, err
	}
	for _, r := range rows {
		switch r.Status {
		case models.StateQueued:
			c.Queued = r.N
		case models.StateRunning:
			c.Running = r.N
		case models.StateSucceeded:
			c.Succeeded = r.N
		case models.StateFailed:
			c.Failed = r.N
		case models.StateDead:
			c.Dead = r.N
		}
	}
	return c, nil
}

// Stats aggregates successful runs, optionally filtered by normalized URL
// and viewport. Missing metric values are excluded from each AVG individually.
func (s *Store) Stats(ctx context.Context, normalizedURL string, vp models.Viewport) (RunStats, error) {
	var st RunStats
	q := s.db.WithContext(ctx).Model(&models.Run{}).Where("status = ?", models.StateSucceeded)
	if normalizedURL != "" {
		q = q.Where("url = ?", normalizedURL)
	}
	if vp != "" {
		q = q.Where("viewport = ?", vp)
	}
	type agg struct {
		N   int64
		Nav *float64
		FCP *float64
		LCP *float64
		CLS *float64
		LT  *float64
	}
	var a agg
	if err := q.Select(`
		count(*) as n,
		avg(nav_duration_ms) as nav,
		avg(fcp_ms) as fcp,
		avg(lcp_ms) as lcp,
		avg(cls) as cls,
		avg(long_task_total_ms) as lt
	`).Scan(&a).Error; err != nil {
		return st, err
	}
	st.SuccessfulRuns = a.N
	st.AvgNavDurationMS = a.Nav
	st.AvgFCPMS = a.FCP
	st.AvgLCPMS = a.LCP
	st.AvgCLS = a.CLS
	st.AvgLongTaskTotal = a.LT
	return st, nil
}
