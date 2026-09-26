// Package tree implements the request cancellation tree.
//
// A request fans out into sibling subtasks. Each subtask performs work
// through the fault-injecting client and may declare a cleanup action. The
// first FATAL failure cancels every sibling subtask (their in-flight HTTP
// calls and any local waits), while the tree still waits for each canceled
// subtask's bounded resource cleanup to finish before reporting. Cancellation
// of the root context (the HTTP client disconnecting) propagates the same
// way: remaining computation stops, cleanups still complete.
package tree

import (
	"time"

	"canceltree/internal/client"
	"canceltree/internal/clock"
)

// Outcome statuses.
const (
	StatusSucceeded = "succeeded"
	StatusFailed    = "failed"
	StatusCanceled  = "canceled"
)

// Tree-level report statuses.
const (
	TreeSucceeded = "succeeded"
	TreeFailed    = "failed"
	TreeCanceled  = "canceled"
)

// Cancellation sources recorded on canceled tasks.
const (
	CanceledByFatalSibling = "fatal-sibling"
	CanceledByClient       = "client-disconnect"
	CanceledByTimeout      = "task-timeout"
)

// CleanupSpec describes a resource-release action for a subtask.
type CleanupSpec struct {
	// URL is called after the subtask stops, on a context detached from
	// the (possibly canceled) request context but bounded by Timeout.
	URL     string        `json:"url"`
	Timeout time.Duration `json:"-"`
}

// TaskSpec is one sibling subtask in the request tree.
type TaskSpec struct {
	ID      string        `json:"id"`
	Call    client.Call   `json:"call"`
	Fatal   bool          `json:"fatal"`
	Timeout time.Duration `json:"-"`
	Cleanup *CleanupSpec  `json:"cleanup,omitempty"`
}

// TaskOutcome is the structured result of one subtask.
type TaskOutcome struct {
	ID           string        `json:"id"`
	Status       string        `json:"status"`
	Fatal        bool          `json:"fatal"`
	Result       client.Result `json:"result"`
	StartedAt    time.Time     `json:"started_at"`
	FinishedAt   time.Time     `json:"finished_at"`
	DurationMS   int64         `json:"duration_ms"`
	CanceledBy   string        `json:"canceled_by,omitempty"`
	CleanupRan   bool          `json:"cleanup_ran"`
	CleanupError string        `json:"cleanup_error,omitempty"`
	CleanupMS    int64         `json:"cleanup_ms"`
}

// Report is the structured result of a whole request tree.
type Report struct {
	RequestID   string        `json:"request_id"`
	Status      string        `json:"status"`
	FatalTaskID string        `json:"fatal_task_id,omitempty"`
	CanceledBy  string        `json:"canceled_by,omitempty"`
	Outcomes    []TaskOutcome `json:"outcomes"`
	StartedAt   time.Time     `json:"started_at"`
	FinishedAt  time.Time     `json:"finished_at"`
	ElapsedMS   int64         `json:"elapsed_ms"`
}

// Config configures the engine.
type Config struct {
	// BaseURL is used to resolve task call/cleanup paths given as paths.
	BaseURL string
	// CleanupTimeout bounds every cleanup action that has no own timeout.
	CleanupTimeout time.Duration
}

// Engine runs request trees.
type Engine struct {
	clk    clock.Clock
	cl     *client.Logic
	base   string
	cleanD time.Duration
}

// NewEngine builds an engine.
func NewEngine(clk clock.Clock, cl *client.Logic, cfg Config) *Engine {
	return &Engine{
		clk:    clk,
		cl:     cl,
		base:   cfg.BaseURL,
		cleanD: orDuration(cfg.CleanupTimeout, 2*time.Second),
	}
}
