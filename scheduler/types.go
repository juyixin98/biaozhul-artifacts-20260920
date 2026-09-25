// Package scheduler implements a DAG job engine with retry budgets,
// dependency-failure propagation (skip vs. fail), pluggable executors and
// clocks, and structured state-change event logs.
//
// The engine runs a single goroutine ("engine loop") that owns every Run/Node
// state transition. Executors are invoked on their own goroutines and report
// results back through a channel, so a node's attempt body never mutates
// engine state directly. This makes the guarantee "a node never runs twice
// concurrently" structural rather than best-effort.
package scheduler

import (
	"context"
	"errors"
	"time"
)

// NodeStatus is the lifecycle state of one node within a run.
type NodeStatus string

const (
	StatusPending   NodeStatus = "pending"
	StatusRunning   NodeStatus = "running"
	StatusWaiting   NodeStatus = "waiting" // attempt failed, backing off before retry
	StatusSucceeded NodeStatus = "succeeded"
	StatusFailed    NodeStatus = "failed"
	StatusSkipped   NodeStatus = "skipped" // never ran: an upstream failure forbade it
	StatusCanceled  NodeStatus = "canceled"
)

func (s NodeStatus) terminal() bool {
	switch s {
	case StatusSucceeded, StatusFailed, StatusSkipped, StatusCanceled:
		return true
	}
	return false
}

// DependencyPolicy decides when an un-succeeded dependency blocks a node.
type DependencyPolicy string

const (
	// PolicyAllSuccess: every dependency must succeed. A failed/skipped/
	// canceled dependency causes the node to be skipped (failed dep) or
	// canceled (canceled dep).
	PolicyAllSuccess DependencyPolicy = "all_success"
	// PolicyAllFinished: the node runs once every dependency reaches a
	// terminal state, regardless of outcome.
	PolicyAllFinished DependencyPolicy = "all_finished"
)

// RunStatus is the aggregate state of a run.
type RunStatus string

const (
	RunPending   RunStatus = "pending"
	RunRunning   RunStatus = "running"
	RunSucceeded RunStatus = "succeeded"
	RunFailed    RunStatus = "failed"
	RunCanceled  RunStatus = "canceled"
)

// EventType enumerates every state transition the engine records.
type EventType string

const (
	EventRunCreated   EventType = "run_created"
	EventRunStarted   EventType = "run_started"
	EventRunSucceeded EventType = "run_succeeded"
	EventRunFailed    EventType = "run_failed"
	EventRunCanceled  EventType = "run_canceled"

	EventNodeQueued    EventType = "node_queued"
	EventNodeStarted   EventType = "node_started"
	EventNodeRetryWait EventType = "node_retry_wait"
	EventNodeRetrying  EventType = "node_retrying"
	EventNodeSucceeded EventType = "node_succeeded"
	EventNodeFailed    EventType = "node_failed"
	EventNodeSkipped   EventType = "node_skipped"
	EventNodeCanceled  EventType = "node_canceled"
)

// Event is one structured, append-only state-change record.
type Event struct {
	Seq              int64                 `json:"seq"`
	Time             time.Time             `json:"time"`
	RunID            string                `json:"run_id"`
	NodeID           string                `json:"node_id,omitempty"`
	Type             EventType             `json:"type"`
	Attempt          int                   `json:"attempt,omitempty"` // 1-based attempt number
	MaxAttempts      int                   `json:"max_attempts,omitempty"`
	Status           NodeStatus            `json:"status,omitempty"`
	RunStatus        RunStatus             `json:"run_status,omitempty"`
	Backoff          time.Duration         `json:"backoff,omitempty"`
	Error            string                `json:"error,omitempty"`
	Reason           string                `json:"reason,omitempty"` // skip/cancel rationale
	DependencyStatus map[string]NodeStatus `json:"dependency_status,omitempty"`
}

// ErrNotFound is returned when a run id is unknown.
var ErrNotFound = errors.New("run not found")

// ErrRunFinished is returned by Cancel when the run already terminated.
var ErrRunFinished = errors.New("run already finished")

// Executor runs one node task attempt. Implementations MUST honor ctx: when
// the run is canceled, ctx is canceled and the attempt should return promptly
// (ctx.Err() or any error marks the node canceled).
type Executor interface {
	Execute(ctx context.Context, in ExecuteInput) error
}

// ExecuteInput is everything an executor gets for one attempt.
type ExecuteInput struct {
	RunID    string
	NodeID   string
	TaskType string
	Params   map[string]string
	Attempt  int // 1-based
}

// ExecutorFunc adapts a function to Executor.
type ExecutorFunc func(ctx context.Context, in ExecuteInput) error

func (f ExecutorFunc) Execute(ctx context.Context, in ExecuteInput) error {
	return f(ctx, in)
}
