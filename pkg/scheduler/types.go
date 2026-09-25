// Package scheduler implements a non-preemptive, multi-resource fair scheduler
// using Dominant Resource Fairness (DRF) across CPU and memory.
//
// The clock (pkg/scheduler/clock.go) and the task executor (executor.go) are
// interfaces so the same scheduler can be driven deterministically in tests
// (FakeClock + SimExecutor) or run against wall time and real OS processes
// (RealClock + ProcessExecutor).
package scheduler

import "time"

// Resources is an allocation request or usage record.
// CPU is in millicpu (1000 == 1 core); Memory is in MiB.
// Integer units keep all DRF bookkeeping exact (no floating point drift).
type Resources struct {
	CPU    int64 `json:"cpu_millicpu"`
	Memory int64 `json:"memory_mib"`
}

// Add returns r+o.
func (r Resources) Add(o Resources) Resources {
	return Resources{CPU: r.CPU + o.CPU, Memory: r.Memory + o.Memory}
}

// Sub returns r-o (may go negative; callers decide whether that is legal).
func (r Resources) Sub(o Resources) Resources {
	return Resources{CPU: r.CPU - o.CPU, Memory: r.Memory - o.Memory}
}

// LessEqual reports whether every dimension of r fits inside o.
func (r Resources) LessEqual(o Resources) bool {
	return r.CPU <= o.CPU && r.Memory <= o.Memory
}

// TaskState is the lifecycle state of a task.
type TaskState string

const (
	StateQueued   TaskState = "QUEUED"   // admitted, waiting for resources
	StateRunning  TaskState = "RUNNING"  // handed to the executor
	StateComplete TaskState = "COMPLETE" // executor reported success
	StateFailed   TaskState = "FAILED"   // executor reported an error
)

// EventType enumerates the structured state-change records emitted by the
// scheduler. Every lifecycle transition produces exactly one event.
type EventType string

const (
	EventSubmitted EventType = "TASK_SUBMITTED"
	EventStarted   EventType = "TASK_STARTED"
	EventWaiting   EventType = "TASK_WAITING" // queued task remained unscheduled after a pass
	EventFinished  EventType = "TASK_FINISHED"
)

// Event is one structured scheduler state change.
type Event struct {
	Seq       int64          `json:"seq"`
	Time      time.Time      `json:"time"`
	Type      EventType      `json:"type"`
	TenantID  string         `json:"tenant_id"`
	TaskID    string         `json:"task_id"`
	Resources Resources      `json:"resources"`
	Detail    map[string]any `json:"detail,omitempty"`
}

// ExecResult is what an executor reports when a running task ends.
type ExecResult struct {
	TaskID string
	Err    error // nil means success
}

// Executor runs tasks and reports their completion.
//
// Start must either return an error synchronously (the task never started,
// resources were never effectively given to it) or arrange for exactly one
// ExecResult to be delivered to Results() later.
type Executor interface {
	Start(t *Task, now time.Time) error
	Results() <-chan ExecResult
}
