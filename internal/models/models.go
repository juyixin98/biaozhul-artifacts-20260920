// Package models defines the GORM-backed persistence model for SiteVitals.
package models

import "time"

// Viewport identifiers supported for analysis.
const (
	ViewportMobile  = "mobile"
	ViewportTablet  = "tablet"
	ViewportDesktop = "desktop"
)

// Job lifecycle states.
const (
	JobQueued    = "queued"
	JobRunning   = "running"
	JobSucceeded = "succeeded"
	JobFailed    = "failed"
)

// Run lifecycle states. A run is one attempt (lease) of a job.
const (
	RunRunning   = "running"
	RunSucceeded = "succeeded"
	RunFailed    = "failed"
)

// Failure classes recorded on failed runs and run events.
const (
	// FailNavigationTimeout: the page navigation did not finish before the deadline.
	FailNavigationTimeout = "navigation_timeout"
	// FailBrowserExited: Chromium crashed, was killed, or the CDP connection dropped.
	FailBrowserExited = "browser_exited"
	// FailPolicy: non-HTTP URL, whitelist violation, or redirect limit exceeded.
	FailPolicy = "policy_violation"
	// FailCollector: metric/result collection itself failed (CDP protocol error).
	FailCollector = "collector_error"
)

// Metric collection status values. Missing values are NEVER replaced with zero.
const (
	MetricCollected   = "collected"
	MetricUnsupported = "unsupported"
	MetricFailed      = "failed"
)

// Site is a registered origin that is allowed to be analysed.
type Site struct {
	ID   uint64 `gorm:"primaryKey;autoIncrement" json:"id"`
	Name string `gorm:"size:128;not null" json:"name"`
	// SchemeHost is the allowed origin, e.g. "http://127.0.0.1:8093".
	SchemeHost  string    `gorm:"size:255;not null;uniqueIndex" json:"scheme_host"`
	Description string    `gorm:"size:512" json:"description"`
	Enabled     bool      `gorm:"not null;default:true" json:"enabled"`
	CreatedAt   time.Time `json:"created_at"`
	UpdatedAt   time.Time `json:"updated_at"`
}

// AllowedURL is one allow-list rule for paths under a site.
type AllowedURL struct {
	ID     uint64 `gorm:"primaryKey;autoIncrement" json:"id"`
	SiteID uint64 `gorm:"not null;index" json:"site_id"`
	// URLPattern is an exact URL or a prefix ending with "*".
	URLPattern string    `gorm:"size:1024;not null" json:"url_pattern"`
	Note       string    `gorm:"size:512" json:"note"`
	Enabled    bool      `gorm:"not null;default:true" json:"enabled"`
	CreatedAt  time.Time `json:"created_at"`
}

// Job is a queued unit of work: analyse one URL with one viewport.
type Job struct {
	ID        uint64 `gorm:"primaryKey;autoIncrement" json:"id"`
	TargetURL string `gorm:"size:1024;not null" json:"target_url"`
	Viewport  string `gorm:"size:16;not null" json:"viewport"`
	Status    string `gorm:"size:16;not null;default:queued;index" json:"status"`
	// Attempts counts how many leases (including the current one) the job had.
	Attempts int `gorm:"not null;default:0" json:"attempts"`
	// MaxAttempts bounds crash-driven retries.
	MaxAttempts int `gorm:"not null;default:3" json:"max_attempts"`

	// Lease fencing: every new lease gets a strictly greater FencingToken.
	LeaseHolder  string     `gorm:"size:128;index" json:"lease_holder"`
	FencingToken int64      `gorm:"not null;default:0" json:"fencing_token"`
	LeasedUntil  *time.Time `json:"leased_until"`

	// SucceededRunID points at the one successful run; exactly one report per job.
	SucceededRunID *uint64 `gorm:"index" json:"succeeded_run_id,omitempty"`
	LastError      string  `gorm:"size:512" json:"last_error,omitempty"`
	LastFailClass  string  `gorm:"size:32" json:"last_fail_class,omitempty"`

	CreatedAt time.Time `json:"created_at"`
	UpdatedAt time.Time `json:"updated_at"`
}

// Run is one attempt at executing a job. FencingToken binds it to one lease.
type Run struct {
	ID           uint64 `gorm:"primaryKey;autoIncrement" json:"id"`
	JobID        uint64 `gorm:"not null;uniqueIndex:idx_job_attempt" json:"job_id"`
	Attempt      int    `gorm:"not null;uniqueIndex:idx_job_attempt" json:"attempt"`
	Viewport     string `gorm:"size:16;not null" json:"viewport"`
	TargetURL    string `gorm:"size:1024;not null" json:"target_url"`
	FinalURL     string `gorm:"size:1024" json:"final_url"`
	Status       string `gorm:"size:16;not null;default:running;index" json:"status"`
	LeaseHolder  string `gorm:"size:128;not null" json:"lease_holder"`
	FencingToken int64  `gorm:"not null" json:"fencing_token"`
	RedirectHops int    `gorm:"not null;default:0" json:"redirect_hops"`
	FailClass    string `gorm:"size:32" json:"fail_class,omitempty"`
	FailMessage  string `gorm:"size:512" json:"fail_message,omitempty"`
	// ResourceFailures counts subresources that failed to load (non-fatal).
	ResourceFailures int `gorm:"not null;default:0" json:"resource_failures"`
	// BlockedResources counts subresources blocked by the allow-list (non-fatal).
	BlockedResources int        `gorm:"not null;default:0" json:"blocked_resources"`
	StartedAt        time.Time  `json:"started_at"`
	FinishedAt       *time.Time `json:"finished_at"`
}

// Metric is one collected Web Vitals value for a successful run.
type Metric struct {
	ID       uint64   `gorm:"primaryKey;autoIncrement" json:"id"`
	RunID    uint64   `gorm:"not null;uniqueIndex:idx_run_metric" json:"run_id"`
	Name     string   `gorm:"size:32;not null;uniqueIndex:idx_run_metric" json:"name"`
	Status   string   `gorm:"size:16;not null" json:"status"`
	ValueMS  *float64 `json:"value_ms,omitempty"`
	ValueCLS *float64 `json:"value_cls,omitempty"`
	// Detail carries structured context (e.g. long-task window) as JSON.
	Detail string `gorm:"type:text" json:"detail,omitempty"`
}

// ResourceEntry is one row of the resource waterfall.
type ResourceEntry struct {
	ID           uint64 `gorm:"primaryKey;autoIncrement" json:"id"`
	RunID        uint64 `gorm:"not null;index" json:"run_id"`
	URL          string `gorm:"size:2048;not null" json:"url"`
	ResourceType string `gorm:"size:32" json:"resource_type"`
	// Initiator is "document", "redirect" or the subresource initiator.
	Initiator     string   `gorm:"size:64" json:"initiator"`
	Status        string   `gorm:"size:16;not null" json:"status"` // loaded|failed|blocked
	HTTPStatus    int      `json:"http_status"`
	StartMS       *float64 `json:"start_ms,omitempty"`
	EndMS         *float64 `json:"end_ms,omitempty"`
	DurationMS    *float64 `json:"duration_ms,omitempty"`
	EncodedBytes  int64    `json:"encoded_bytes"`
	BlockedReason string   `gorm:"size:255" json:"blocked_reason,omitempty"`
}

// RunEvent records notable events (redirects, blocks, warnings, failure detail).
type RunEvent struct {
	ID        uint64    `gorm:"primaryKey;autoIncrement" json:"id"`
	RunID     uint64    `gorm:"not null;index" json:"run_id"`
	Kind      string    `gorm:"size:32;not null" json:"kind"`
	Message   string    `gorm:"size:1024;not null" json:"message"`
	CreatedAt time.Time `json:"created_at"`
}

// Budget is a per-URL/per-viewport threshold definition.
// NOTE: the (target_url, viewport, metric) unique index with a 191-char
// prefix is created by the versioned SQL migration; GORM's auto-migrator
// cannot express prefix lengths so it is not tagged here.
type Budget struct {
	ID        uint64 `gorm:"primaryKey;autoIncrement" json:"id"`
	TargetURL string `gorm:"size:1024;not null" json:"target_url"`
	Viewport  string `gorm:"size:16;not null;default:''" json:"viewport"`
	Metric    string `gorm:"size:32;not null" json:"metric"`
	// ThresholdMS is used for millisecond metrics; ThresholdCLS for CLS.
	ThresholdMS  *float64  `json:"threshold_ms,omitempty"`
	ThresholdCLS *float64  `json:"threshold_cls,omitempty"`
	Enabled      bool      `gorm:"not null;default:true" json:"enabled"`
	CreatedAt    time.Time `json:"created_at"`
	UpdatedAt    time.Time `json:"updated_at"`
}

// BudgetEvaluation records one budget check against one successful run.
type BudgetEvaluation struct {
	ID           uint64   `gorm:"primaryKey;autoIncrement" json:"id"`
	BudgetID     uint64   `gorm:"not null;index" json:"budget_id"`
	RunID        uint64   `gorm:"not null;index" json:"run_id"`
	JobID        uint64   `gorm:"not null;index" json:"job_id"`
	TargetURL    string   `gorm:"size:1024;not null" json:"target_url"`
	Viewport     string   `gorm:"size:16;not null" json:"viewport"`
	Metric       string   `gorm:"size:32;not null" json:"metric"`
	ActualMS     *float64 `json:"actual_ms,omitempty"`
	ActualCLS    *float64 `json:"actual_cls,omitempty"`
	ThresholdMS  *float64 `json:"threshold_ms,omitempty"`
	ThresholdCLS *float64 `json:"threshold_cls,omitempty"`
	// Exceeded is true when an actual value crossed the threshold.
	Exceeded bool `gorm:"not null" json:"exceeded"`
	// Skipped is true when the metric was missing/unsupported: no pass/fail claim.
	Skipped   bool      `gorm:"not null;default:false" json:"skipped"`
	CreatedAt time.Time `json:"created_at"`
}

// AllModels returns every model for AutoMigrate.
func AllModels() []any {
	return []any{
		&Site{}, &AllowedURL{}, &Job{}, &Run{}, &Metric{},
		&ResourceEntry{}, &RunEvent{}, &Budget{}, &BudgetEvaluation{},
	}
}
