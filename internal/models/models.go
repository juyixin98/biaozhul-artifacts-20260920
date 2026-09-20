package models

import (
	"time"

	"gorm.io/gorm"
)

// Viewports supported by the collector. Width/Height/Density are device
// metrics; UserAgentType selects a realistic UA string per viewport.
type Viewport string

const (
	ViewportMobile  Viewport = "mobile"
	ViewportTablet  Viewport = "tablet"
	ViewportDesktop Viewport = "desktop"
)

func (v Viewport) Valid() bool {
	switch v {
	case ViewportMobile, ViewportTablet, ViewportDesktop:
		return true
	}
	return false
}

// Task/run lifecycle states.
const (
	StateQueued    = "queued"
	StateRunning   = "running"
	StateSucceeded = "succeeded"
	StateFailed    = "failed"
	StateDead      = "dead"
	StateAbandoned = "abandoned" // an earlier attempt whose lease died while it was running
)

// Metric status values. Unsupported means the browser/API could not produce
// the metric at all; partial means timing data was restricted (e.g. cross
// origin without Timing-Allow-Origin).
const (
	MetricOK          = "ok"
	MetricUnsupported = "unsupported"
	MetricPartial     = "partial"
	MetricFailed      = "failed"
)

// Site is a whitelisted origin+path-prefix entry. Every navigation and every
// redirect hop must match at least one enabled site entry.
type Site struct {
	ID          uint           `gorm:"primaryKey" json:"id"`
	Name        string         `gorm:"size:128;not null" json:"name"`
	Origin      string         `gorm:"size:255;not null;uniqueIndex:uniq_sites_origin_prefix" json:"origin"`
	PathPrefix  string         `gorm:"size:512;not null;default:/;uniqueIndex:uniq_sites_origin_prefix" json:"path_prefix"`
	Description string         `gorm:"size:512" json:"description"`
	Enabled     bool           `gorm:"not null;default:true" json:"enabled"`
	CreatedAt   time.Time      `json:"created_at"`
	UpdatedAt   time.Time      `json:"updated_at"`
	DeletedAt   gorm.DeletedAt `gorm:"index" json:"-"`
}

func (s Site) TableName() string { return "sites" }

// Task is the durable unit of work on the queue.
type Task struct {
	ID          uint     `gorm:"primaryKey" json:"id"`
	URL         string   `gorm:"size:2048;not null" json:"url"`
	FinalURL    string   `gorm:"size:2048" json:"final_url,omitempty"`
	Viewport    Viewport `gorm:"size:16;not null" json:"viewport"`
	Status      string   `gorm:"size:16;not null;index:idx_tasks_status" json:"status"`
	Priority    int      `gorm:"not null;default:0" json:"priority"`
	Attempts    int      `gorm:"not null;default:0" json:"attempts"`
	MaxAttempts int      `gorm:"not null;default:3" json:"max_attempts"`
	// Lease fields. Owner is the worker instance id; LeaseToken is rotated
	// every claim and guards late commits from crashed/old executors.
	Owner       *string        `gorm:"size:64" json:"owner,omitempty"`
	LeaseToken  *string        `gorm:"size:64" json:"-"`
	LeasedUntil *time.Time     `gorm:"index" json:"leased_until,omitempty"`
	RunAfter    time.Time      `gorm:"not null;index:idx_tasks_run_after" json:"run_after"`
	StartedAt   *time.Time     `json:"started_at,omitempty"`
	FinishedAt  *time.Time     `json:"finished_at,omitempty"`
	ErrorCode   string         `gorm:"size:64" json:"error_code,omitempty"`
	ErrorMsg    string         `gorm:"size:1024" json:"error_msg,omitempty"`
	CreatedAt   time.Time      `json:"created_at"`
	UpdatedAt   time.Time      `json:"updated_at"`
	DeletedAt   gorm.DeletedAt `gorm:"index" json:"-"`
}

func (t Task) TableName() string { return "tasks" }

// Run is one execution attempt of a task. Failed/abandoned runs are retained
// for diagnostics but never mixed into successful-metric statistics.
type Run struct {
	ID         uint       `gorm:"primaryKey" json:"id"`
	TaskID     uint       `gorm:"not null;uniqueIndex:uniq_runs_task_attempt,priority:1" json:"task_id"`
	AttemptNo  int        `gorm:"not null;uniqueIndex:uniq_runs_task_attempt,priority:2" json:"attempt_no"`
	Status     string     `gorm:"size:16;not null;index" json:"status"`
	Owner      string     `gorm:"size:64" json:"owner"`
	Viewport   Viewport   `gorm:"size:16;not null" json:"viewport"`
	URL        string     `gorm:"size:2048;not null" json:"url"`
	FinalURL   string     `gorm:"size:2048" json:"final_url,omitempty"`
	StartedAt  time.Time  `gorm:"not null" json:"started_at"`
	FinishedAt *time.Time `json:"finished_at,omitempty"`

	// Timing metrics in milliseconds; CLS is unitless. Null means the metric
	// was not produced (unsupported / failed / partial restriction) — never 0.
	NavDurationMS   *float64 `gorm:"type:decimal(12,3)" json:"nav_duration_ms"`
	FCPMS           *float64 `gorm:"column:fcp_ms;type:decimal(12,3)" json:"fcp_ms"`
	LCPMS           *float64 `gorm:"column:lcp_ms;type:decimal(12,3)" json:"lcp_ms"`
	CLS             *float64 `gorm:"type:decimal(12,4)" json:"cls"`
	LongTaskCount   *int     `json:"long_task_count"`
	LongTaskTotalMS *float64 `gorm:"type:decimal(12,3)" json:"long_task_total_ms"`
	LongTaskMaxMS   *float64 `gorm:"type:decimal(12,3)" json:"long_task_max_ms"`
	// Long task observation window: [started_at + window_start_offset_ms,
	// window_end]. Long tasks can only be observed while the injected
	// PerformanceObserver lives, i.e. from document creation through harvest.
	WindowStartOffsetMS float64    `gorm:"type:decimal(12,3);not null;default:0" json:"window_start_offset_ms"`
	WindowEnd           *time.Time `json:"window_end,omitempty"`

	MetricStatusJSON string      `gorm:"type:longtext" json:"-"`
	MetricStatus     MetricSet   `gorm:"-" json:"metric_status"`
	WarningsJSON     string      `gorm:"type:longtext" json:"-"`
	Warnings         []string    `gorm:"-" json:"warnings"`
	RedirectsJSON    string      `gorm:"type:longtext" json:"-"`
	Redirects        []Redirect  `gorm:"-" json:"redirects"`
	ViolationsJSON   string      `gorm:"type:longtext" json:"-"`
	Violations       []Violation `gorm:"-" json:"violations"`
	ResourcesJSON    string      `gorm:"type:longtext" json:"-"`
	Resources        []Resource  `gorm:"-" json:"resources"`

	ErrorCode string `gorm:"size:64;index" json:"error_code,omitempty"`
	ErrorMsg  string `gorm:"size:1024" json:"error_msg,omitempty"`

	CreatedAt time.Time `json:"created_at"`
	UpdatedAt time.Time `json:"updated_at"`
}

func (r Run) TableName() string { return "runs" }

// MetricSet records how each metric was produced, so a missing value is
// explicit instead of being silently rendered as zero.
type MetricSet struct {
	Navigation string `json:"navigation"`
	FCP        string `json:"fcp"`
	LCP        string `json:"lcp"`
	CLS        string `json:"cls"`
	LongTasks  string `json:"long_tasks"`
	Resources  string `json:"resources"`
}

// Redirect is one hop of the main-document redirect chain.
type Redirect struct {
	URL         string `json:"url"`
	Status      int64  `json:"status"`
	Allowed     bool   `json:"allowed"`
	Blocked     bool   `json:"blocked,omitempty"`
	Whitelisted bool   `json:"whitelisted"`
}

// Violation records a subresource (or document) request that left the
// whitelist or used a non-HTTP scheme. In non-strict mode subresource
// violations are allowed to load but recorded; in strict mode they are blocked.
type Violation struct {
	URL     string `json:"url"`
	Kind    string `json:"kind"`  // "scheme" | "not_whitelisted"
	Stage   string `json:"stage"` // "document" | "subresource" | "redirect"
	Blocked bool   `json:"blocked"`
}

// Resource is one entry of the resource waterfall.
type Resource struct {
	URL              string   `json:"url"`
	Type             string   `json:"type"`
	Method           string   `json:"method"`
	Status           int64    `json:"status"` // 0 when no response was received
	FromCache        bool     `json:"from_cache"`
	Failed           bool     `json:"failed"`
	Blocked          bool     `json:"blocked"`
	FailureText      string   `json:"failure_text,omitempty"`
	MimeType         string   `json:"mime_type,omitempty"`
	StartMS          float64  `json:"start_ms"` // offset from navigationStart
	DNSMS            *float64 `json:"dns_ms,omitempty"`
	ConnectMS        *float64 `json:"connect_ms,omitempty"`
	SSLMS            *float64 `json:"ssl_ms,omitempty"`
	RequestMS        *float64 `json:"request_ms,omitempty"`  // request sent -> TTFB
	ResponseMS       *float64 `json:"response_ms,omitempty"` // TTFB -> download end
	DurationMS       float64  `json:"duration_ms"`
	TimingRestricted bool     `json:"timing_restricted"`
}

// Report is the human-readable artifact of a successful run. Exactly one
// exists per successful task (unique task_id enforced at DB level).
type Report struct {
	ID        uint      `gorm:"primaryKey" json:"id"`
	TaskID    uint      `gorm:"not null;uniqueIndex:uniq_reports_task" json:"task_id"`
	RunID     uint      `gorm:"not null;uniqueIndex:uniq_reports_run" json:"run_id"`
	Markdown  string    `gorm:"type:longtext;not null" json:"markdown"`
	CreatedAt time.Time `json:"created_at"`
}

func (r Report) TableName() string { return "reports" }

// Budget holds alert thresholds for the global defaults (SiteID == 0) or a
// per-site override.
type Budget struct {
	ID              uint      `gorm:"primaryKey" json:"id"`
	SiteID          uint      `gorm:"not null;default:0;uniqueIndex:uniq_budgets_site" json:"site_id"`
	FCPMS           *float64  `gorm:"column:fcp_ms;type:decimal(12,3)" json:"fcp_ms,omitempty"`
	LCPMS           *float64  `gorm:"column:lcp_ms;type:decimal(12,3)" json:"lcp_ms,omitempty"`
	CLS             *float64  `gorm:"type:decimal(12,4)" json:"cls,omitempty"`
	NavDurationMS   *float64  `gorm:"type:decimal(12,3)" json:"nav_duration_ms,omitempty"`
	LongTaskTotalMS *float64  `gorm:"type:decimal(12,3)" json:"long_task_total_ms,omitempty"`
	CreatedAt       time.Time `json:"created_at"`
	UpdatedAt       time.Time `json:"updated_at"`
}

func (b Budget) TableName() string { return "budgets" }

// BudgetAlert records one threshold breach, always linked to the run whose
// actual value crossed it.
type BudgetAlert struct {
	ID        uint      `gorm:"primaryKey" json:"id"`
	RunID     uint      `gorm:"not null;index:idx_budget_alerts_run" json:"run_id"`
	TaskID    uint      `gorm:"not null;index" json:"task_id"`
	Metric    string    `gorm:"size:32;not null" json:"metric"`
	Actual    float64   `gorm:"type:decimal(12,4);not null" json:"actual"`
	Threshold float64   `gorm:"type:decimal(12,4);not null" json:"threshold"`
	Severity  string    `gorm:"size:16;not null" json:"severity"` // warn
	CreatedAt time.Time `json:"created_at"`
}

func (b BudgetAlert) TableName() string { return "budget_alerts" }

// Comparison is the persisted result of diffing two successful runs of the
// same normalized URL + viewport.
type Comparison struct {
	ID          uint       `gorm:"primaryKey" json:"id"`
	URL         string     `gorm:"size:2048;not null" json:"url"`
	Viewport    Viewport   `gorm:"size:16;not null" json:"viewport"`
	BaselineRun uint       `gorm:"not null" json:"baseline_run"`
	CurrentRun  uint       `gorm:"not null" json:"current_run"`
	DiffJSON    string     `gorm:"type:json;not null" json:"-"`
	Diff        MetricDiff `gorm:"-" json:"diff"`
	CreatedAt   time.Time  `json:"created_at"`
}

func (c Comparison) TableName() string { return "comparisons" }

// MetricDiff is the per-metric delta (current - baseline). Missing values on
// either side are reported as such rather than coerced to zero.
type MetricDiff struct {
	NavDurationMS   Delta `json:"nav_duration_ms"`
	FCPMS           Delta `json:"fcp_ms"`
	LCPMS           Delta `json:"lcp_ms"`
	CLS             Delta `json:"cls"`
	LongTaskCount   Delta `json:"long_task_count"`
	LongTaskTotalMS Delta `json:"long_task_total_ms"`
}

// Delta is one numeric comparison. BothBaselineMissing/BothCurrentMissing
// signal that no delta could be computed.
type Delta struct {
	Baseline        *float64 `json:"baseline"`
	Current         *float64 `json:"current"`
	Change          *float64 `json:"change"`
	PercentChange   *float64 `json:"percent_change"`
	BaselineMissing bool     `json:"baseline_missing"`
	CurrentMissing  bool     `json:"current_missing"`
	Regressed       bool     `json:"regressed"`
}
