// Package model defines the wire-level and persistence data model for the
// resumable DAG executor. It has no dependencies on the other internal
// packages so that storage, engine and server can all share these types.
package model

import "time"

// DAG is the user-supplied specification of a computation graph.
type DAG struct {
	// Name is an optional human-readable label.
	Name string `json:"name,omitempty"`
	// Retries is the DEFAULT number of retries (attempts after the first one)
	// for nodes that do not specify their own value. 0 means run once only.
	Retries int `json:"retries,omitempty"`
	// BackoffMs is the DEFAULT delay between attempts in milliseconds.
	BackoffMs int `json:"backoff_ms,omitempty"`
	// Nodes is the set of tasks. Order is irrelevant; dependencies form the
	// edges and must describe an acyclic graph.
	Nodes []Node `json:"nodes"`
}

// Node is one task in a DAG.
type Node struct {
	// ID is unique within the DAG and matches ^[A-Za-z0-9_-]{1,64}$.
	ID string `json:"id"`
	// Type selects a function from the server-side whitelist.
	Type string `json:"type"`
	// Params are static parameters. A parameter value may be a reference of
	// the form {"$ref": "otherNodeID"}, which is replaced with that node's
	// result immediately before execution.
	Params map[string]any `json:"params,omitempty"`
	// Deps lists node IDs that must succeed before this node may run.
	Deps []string `json:"deps,omitempty"`
	// Retries / BackoffMs override the DAG-level defaults when non-nil.
	Retries   *int `json:"retries,omitempty"`
	BackoffMs *int `json:"backoff_ms,omitempty"`
}

// Status of a node or of a DAG.
type Status string

const (
	// Node states.
	StatusPending   Status = "pending"   // defined, not yet eligible or not yet launched
	StatusRunning   Status = "running"   // a function invocation is in flight
	StatusRetrying  Status = "retrying"  // last attempt failed, waiting for backoff
	StatusSucceeded Status = "succeeded" // confirmed success, result cached
	StatusFailed    Status = "failed"    // all attempts exhausted
	StatusCancelled Status = "cancelled" // never started because the DAG was cancelled
	StatusSkipped   Status = "skipped"   // never started because a dependency failed/was skipped

	// DAG states reuse StatusRunning/Succeeded/Failed/Cancelled.
)

// Terminal reports whether a DAG status is final.
func (s Status) Terminal() bool {
	return s == StatusSucceeded || s == StatusFailed || s == StatusCancelled
}

// NodeState is the persisted runtime state of one node.
type NodeState struct {
	Status    Status     `json:"status"`
	Attempts  int        `json:"attempts"` // number of times a function was launched
	Result    any        `json:"result,omitempty"`
	Error     string     `json:"error,omitempty"`
	RunAfter  *time.Time `json:"run_after,omitempty"` // retry becomes eligible at this time
	StartedAt *time.Time `json:"started_at,omitempty"`
	UpdatedAt time.Time  `json:"updated_at"`
}

// Snapshot is the full persisted state of one DAG run. The executor can be
// reconstructed from nothing but this record.
type Snapshot struct {
	ID              string                `json:"id"`
	Spec            DAG                   `json:"spec"`
	Status          Status                `json:"status"`
	CancelRequested bool                  `json:"cancel_requested"`
	Nodes           map[string]*NodeState `json:"nodes"`
	CreatedAt       time.Time             `json:"created_at"`
	UpdatedAt       time.Time             `json:"updated_at"`
}
