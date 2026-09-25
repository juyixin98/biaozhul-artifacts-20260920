// Package scheduler implements a DAG job engine with retry budgets,
// dependency-failure propagation (skip vs fail), and structured event logs.
//
// The two side effects of running a job are injected behind interfaces:
//   - Executor performs a node's work;
//   - Clock measures retry backoff.
//
// Both can be replaced in tests with controllable implementations.
package scheduler

import "time"

// NodeState is the lifecycle state of a single DAG node.
type NodeState string

const (
	NodePending   NodeState = "PENDING"   // not yet eligible / waiting on deps
	NodeRunning   NodeState = "RUNNING"   // Executor call in flight
	NodeRetrying  NodeState = "RETRYING"  // attempt failed, waiting for backoff
	NodeSucceeded NodeState = "SUCCEEDED" // terminal
	NodeFailed    NodeState = "FAILED"    // terminal (retries exhausted)
	NodeSkipped   NodeState = "SKIPPED"   // terminal (dependency policy)
)

// Terminal reports whether the state is a terminal state.
func (s NodeState) Terminal() bool {
	switch s {
	case NodeSucceeded, NodeFailed, NodeSkipped:
		return true
	}
	return false
}

// JobState is the state of a whole job.
type JobState string

const (
	JobRunning   JobState = "RUNNING"
	JobSucceeded JobState = "SUCCEEDED"
	JobFailed    JobState = "FAILED"
	JobCanceled  JobState = "CANCELED"
)

// DepPolicy decides how a node reacts to finished dependency nodes.
type DepPolicy string

const (
	// RequireAllSuccess: the node runs only when all deps succeeded.
	// Any failed dep fails the node; a skipped dep skips it.
	RequireAllSuccess DepPolicy = "ALL_SUCCESS"
	// RequireAllEnd: the node runs once every dep reached a terminal state,
	// regardless of success/failure/skip.
	RequireAllEnd DepPolicy = "ALL_END"
)

// NodeSpec declares one node of a DAG.
type NodeSpec struct {
	// Name is the unique node identifier within the job.
	Name string `json:"name"`
	// Deps lists node names that must finish (per DepPolicy) first.
	Deps []string `json:"deps"`
	// Policy applies to this node's dependencies. Defaults to ALL_SUCCESS.
	Policy DepPolicy `json:"policy"`
	// MaxAttempts bounds total Executor calls for this node (>=1).
	// A value of 0 is treated as 1.
	MaxAttempts int `json:"maxAttempts"`
	// Payload is interpreted by the configured Executor.
	Payload map[string]any `json:"payload,omitempty"`
}

// DagSpec is the static description of a job submitted to the engine.
type DagSpec struct {
	// Name is an optional human-readable job name.
	Name  string     `json:"name,omitempty"`
	Nodes []NodeSpec `json:"nodes"`
}

// Executor performs node work. Implementations must return the call
// promptly when ctx is canceled. The engine never invokes the same node
// concurrently: at most one Execute call for a given (jobID, nodeName)
// exists at any instant. Attempt is 1-based.
type Executor interface {
	Execute(ctx *ExecContext, attempt int) error
}

// ExecContext is passed to every Executor.Execute call.
type ExecContext struct {
	JobID string
	Node  *NodeSpec
	// Ctx is canceled when the job is canceled or the retry wait is aborted.
	Ctx DoneContext
}

// DoneContext is the cancellation surface executors see.
type DoneContext interface {
	Done() <-chan struct{}
	Err() error
}

// Clock abstracts time for retry backoff so tests can advance it deterministically.
type Clock interface {
	// Now returns the current time.
	Now() time.Time
	// Sleep blocks for d or until ctx is canceled. It returns false when
	// canceled before the delay elapsed.
	Sleep(ctx DoneContext, d time.Duration) bool
}

// EventType enumerates structured state-change events.
type EventType string

const (
	EventJobSubmitted  EventType = "JOB_SUBMITTED"
	EventJobStarted    EventType = "JOB_STARTED"
	EventNodeQueued    EventType = "NODE_QUEUED"
	EventNodeStarted   EventType = "NODE_STARTED"
	EventNodeRetry     EventType = "NODE_RETRY_WAIT"
	EventNodeSucceeded EventType = "NODE_SUCCEEDED"
	EventNodeFailed    EventType = "NODE_FAILED"
	EventNodeSkipped   EventType = "NODE_SKIPPED"
	EventJobSucceeded  EventType = "JOB_SUCCEEDED"
	EventJobFailed     EventType = "JOB_FAILED"
	EventJobCanceled   EventType = "JOB_CANCELED"
)

// Event is one structured state-change record. Events for a single job are
// emitted in causal order and carry monotonically increasing Seq values
// (unique per job). Time comes from the engine Clock.
type Event struct {
	Seq      int            `json:"seq"`
	Time     time.Time      `json:"time"`
	Type     EventType      `json:"type"`
	JobID    string         `json:"jobId"`
	JobName  string         `json:"jobName,omitempty"`
	Node     string         `json:"node,omitempty"`
	Attempt  int            `json:"attempt,omitempty"`
	State    NodeState      `json:"state,omitempty"`
	Error    string         `json:"error,omitempty"`
	Reason   string         `json:"reason,omitempty"`
	Backoff  time.Duration  `json:"backoff,omitempty"`
	JobState JobState       `json:"jobState,omitempty"`
	Detail   map[string]any `json:"detail,omitempty"`
}

// Sink receives events. Implementations must be safe for concurrent use
// (multiple jobs emit concurrently); per-job ordering is preserved.
type Sink interface {
	Record(e Event)
}

// SinkFunc adapts a function into a Sink.
type SinkFunc func(Event)

func (f SinkFunc) Record(e Event) { f(e) }

// NodeStatus is an immutable snapshot of node progress.
type NodeStatus struct {
	Name        string    `json:"name"`
	State       NodeState `json:"state"`
	Attempts    int       `json:"attempts"`
	MaxAttempts int       `json:"maxAttempts"`
	Policy      DepPolicy `json:"policy"`
	Deps        []string  `json:"deps"`
	LastError   string    `json:"lastError,omitempty"`
	StartedAt   time.Time `json:"startedAt,omitempty"`
	EndedAt     time.Time `json:"endedAt,omitempty"`
}
