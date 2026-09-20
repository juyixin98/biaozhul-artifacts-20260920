package store

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"time"

	"gorm.io/gorm"

	"sitevitals/internal/models"
)

// RunMetrics is the collector output persisted onto a run. All metric values
// use pointers so "not measured" stays NULL rather than becoming zero.
type RunMetrics struct {
	FinalURL string

	NavDurationMS   *float64
	FCPMS           *float64
	LCPMS           *float64
	CLS             *float64
	LongTaskCount   *int
	LongTaskTotalMS *float64
	LongTaskMaxMS   *float64

	// WindowStartOffsetMS is always 0 (observer is installed before any page
	// script runs). WindowEnd marks harvest time in wall clock.
	WindowStartOffsetMS float64
	WindowEnd           time.Time

	MetricStatus models.MetricSet
	Warnings     []string
	Redirects    []models.Redirect
	Violations   []models.Violation
	Resources    []models.Resource
}

// ColumnUpdates maps collector output onto run columns/JSON blobs.
func (m *RunMetrics) ColumnUpdates(now time.Time) map[string]interface{} {
	statusJSON, _ := json.Marshal(m.MetricStatus)
	warnJSON, _ := json.Marshal(m.Warnings)
	redJSON, _ := json.Marshal(m.Redirects)
	violJSON, _ := json.Marshal(m.Violations)
	resJSON, _ := json.Marshal(m.Resources)
	// A zero time serializes to '0000-00-00' which MySQL strict mode rejects;
	// NULL is the correct "window end unknown" value.
	var windowEnd interface{}
	if !m.WindowEnd.IsZero() {
		windowEnd = m.WindowEnd
	}
	return map[string]interface{}{
		"nav_duration_ms":        m.NavDurationMS,
		"fcp_ms":                 m.FCPMS,
		"lcp_ms":                 m.LCPMS,
		"cls":                    m.CLS,
		"long_task_count":        m.LongTaskCount,
		"long_task_total_ms":     m.LongTaskTotalMS,
		"long_task_max_ms":       m.LongTaskMaxMS,
		"window_start_offset_ms": m.WindowStartOffsetMS,
		"window_end":             windowEnd,
		"metric_status_json":     string(statusJSON),
		"warnings_json":          string(warnJSON),
		"redirects_json":         string(redJSON),
		"violations_json":        string(violJSON),
		"resources_json":         string(resJSON),
		"finished_at":            now,
		"updated_at":             now,
	}
}

// hydrateRun decodes the JSON sidecar columns into the struct fields.
func hydrateRun(r *models.Run) error {
	if r.MetricStatusJSON != "" {
		if err := json.Unmarshal([]byte(r.MetricStatusJSON), &r.MetricStatus); err != nil {
			return err
		}
	}
	if r.WarningsJSON != "" {
		_ = json.Unmarshal([]byte(r.WarningsJSON), &r.Warnings)
	}
	if r.RedirectsJSON != "" {
		_ = json.Unmarshal([]byte(r.RedirectsJSON), &r.Redirects)
	}
	if r.ViolationsJSON != "" {
		_ = json.Unmarshal([]byte(r.ViolationsJSON), &r.Violations)
	}
	if r.ResourcesJSON != "" {
		_ = json.Unmarshal([]byte(r.ResourcesJSON), &r.Resources)
	}
	return nil
}

func strPtr(s string) *string { return &s }

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n-3] + "..."
}

func newLeaseToken() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

// backoff grows with attempts: 5s, 10s, 20s ... capped at 2 minutes. Callers
// may still poll earlier; this only schedules re-delivery.
func backoff(attempt int) time.Duration {
	d := 5 * time.Second
	for i := 1; i < attempt; i++ {
		d *= 2
		if d >= 2*time.Minute {
			return 2 * time.Minute
		}
	}
	return d
}

// IsNotFound / IsLeaseLost convenience predicates.
func IsNotFound(err error) bool  { return errors.Is(err, ErrNotFound) }
func IsLeaseLost(err error) bool { return errors.Is(err, ErrLeaseLost) }

var _ = gorm.ErrRecordNotFound
