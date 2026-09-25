// Package scheduler implements a two-dimensional (CPU, memory) weighted
// Dominant Resource Fairness (DRF) scheduler.
//
// Design notes:
//   - Resources use integer units (milliCPU, MiB), so allocation bookkeeping
//     is exact and the scheduler never over-commits.
//   - Tasks are non-preemptible: once a task is started it keeps its
//     resources until the Executor reports it finished.
//   - Tenants have explicit integer weights (default 1). A task whose
//     request exceeds total cluster capacity permanently blocks its own
//     tenant queue; it never blocks other tenants.
//   - Every state change appends one structured Event to the EventStore.
//   - Clock and Executor are interfaces, so the same scheduler can run
//     against wall time or a fully deterministic fake clock in tests.
package scheduler

import (
	"encoding/json"
	"errors"
	"time"
)

// Resources is the two-dimensional resource vector.
// CPU is measured in milliCPU (1000 == one core), Mem in MiB.
type Resources struct {
	CPU int64 `json:"cpu_milli"`
	Mem int64 `json:"mem_mib"`
}

func (r Resources) add(o Resources) Resources {
	return Resources{CPU: r.CPU + o.CPU, Mem: r.Mem + o.Mem}
}

func (r Resources) sub(o Resources) Resources {
	return Resources{CPU: r.CPU - o.CPU, Mem: r.Mem - o.Mem}
}

// fitsIn reports whether r component-wise fits inside limit.
func (r Resources) fitsIn(limit Resources) bool {
	return r.CPU <= limit.CPU && r.Mem <= limit.Mem
}

// Duration wraps time.Duration so it (un)marshals as e.g. "3s".
type Duration struct {
	time.Duration
}

func (d Duration) MarshalJSON() ([]byte, error) {
	return json.Marshal(d.String())
}

func (d *Duration) UnmarshalJSON(b []byte) error {
	var v any
	if err := json.Unmarshal(b, &v); err != nil {
		return err
	}
	switch t := v.(type) {
	case string:
		parsed, err := time.ParseDuration(t)
		if err != nil {
			return err
		}
		d.Duration = parsed
		return nil
	case float64:
		d.Duration = time.Duration(t)
		return nil
	default:
		return errors.New("duration must be a duration string (e.g. \"3s\") or integer nanoseconds")
	}
}

// TaskStatus is the lifecycle state of a task.
type TaskStatus string

const (
	StatusWaiting TaskStatus = "WAITING"
	StatusRunning TaskStatus = "RUNNING"
	StatusDone    TaskStatus = "FINISHED"
)

// EventType enumerates every recorded state transition.
type EventType string

const (
	EventTenantCreated EventType = "TENANT_CREATED"
	EventTaskSubmitted EventType = "TASK_SUBMITTED"
	EventTaskStarted   EventType = "TASK_STARTED"
	EventTaskFinished  EventType = "TASK_FINISHED"
	EventTaskBlocked   EventType = "TASK_BLOCKED" // request > total capacity
)

// Event is one structured, append-only state-change record.
// EventID is a monotonically increasing sequence number (1-based).
// Fields not relevant to an event type keep their zero values.
type Event struct {
	EventID   int64      `json:"event_id"`
	Seq       int64      `json:"seq"` // global submission sequence of the task, 0 for non-task events
	Type      EventType  `json:"type"`
	At        time.Time  `json:"at"`
	TenantID  string     `json:"tenant_id,omitempty"`
	TaskID    string     `json:"task_id,omitempty"`
	Request   *Resources `json:"request,omitempty"`
	Used      *Resources `json:"used_after,omitempty"` // total cluster usage after the change
	Available *Resources `json:"available_after,omitempty"`
	Reason    string     `json:"reason,omitempty"`
}

// Sentinel errors returned by scheduler operations.
var (
	ErrTenantExists   = errors.New("tenant already exists")
	ErrTenantNotFound = errors.New("tenant not found")
	ErrTaskExists     = errors.New("task already exists")
	ErrTaskNotFound   = errors.New("task not found")
	ErrBadRequest     = errors.New("invalid request")
)
