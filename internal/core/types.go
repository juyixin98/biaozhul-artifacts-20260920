package core

import (
	"time"
)

// Ref identifies a resource by kind and name.
type Ref struct {
	Kind string `json:"kind"`
	Name string `json:"name"`
}

// ResourceView is a registered tool/station with its current holder.
type ResourceView struct {
	Ref
	Description string     `json:"description"`
	HeldBy      *HoldBrief `json:"held_by"`
}

// HoldBrief is the holder summary embedded in a resource view.
type HoldBrief struct {
	TaskID     int64     `json:"task_id"`
	TaskLabel  string    `json:"task_label"`
	RequestID  int64     `json:"request_id"`
	AcquiredAt time.Time `json:"acquired_at"`
}

// CreateTaskInput is the body for POST /tasks.
type CreateTaskInput struct {
	Label       string  `json:"label"`
	Priority    int     `json:"priority"`
	AgingPerSec float64 `json:"aging_per_sec"` // negative = service default
	TimeoutMS   int64   `json:"timeout_ms"`
	Resources   []Ref   `json:"resources"`
}

// ExtraRequestInput is the body for POST /tasks/{id}/requests.
type ExtraRequestInput struct {
	Resources []Ref `json:"resources"`
}

// RevokeInput is the body for POST /tasks/{id}/revoke.
//
// Every resource listed here is asserted by the caller (the operator /
// robot controller) to be confirmed stopped. The service never releases a
// resource speculatively.
type RevokeInput struct {
	Resources []Ref  `json:"resources"`
	Reason    string `json:"reason"`
}

// HeartbeatInput optionally overrides the lease extension.
type HeartbeatInput struct {
	ExtendMS *int64 `json:"extend_ms"`
}

// WaitReason explains one blocked edge of a waiting task.
type WaitReason struct {
	Resource     Ref       `json:"resource"`
	HolderTaskID int64     `json:"holder_task_id"`
	HolderLabel  string    `json:"holder_label"`
	HolderState  string    `json:"holder_state"`
	Since        time.Time `json:"since"`
}

// AllocationResult is returned by task creation and dynamic requests.
type AllocationResult struct {
	TaskID            int64        `json:"task_id"`
	RequestID         int64        `json:"request_id"`
	Granted           bool         `json:"granted"`
	State             string       `json:"state"`
	HeldResources     []Ref        `json:"held_resources"`
	AcquiredResources []Ref        `json:"acquired_resources"`
	EffectivePriority float64      `json:"effective_priority"`
	QueuePosition     int          `json:"queue_position"`
	WaitReasons       []WaitReason `json:"wait_reasons"`
	Cycle             []string     `json:"cycle,omitempty"`
	EvidenceIDs       []int64      `json:"evidence_ids"`
	Deadline          *time.Time   `json:"deadline,omitempty"`
}

// TaskView is the external representation of a task.
type TaskView struct {
	ID                 int64        `json:"id"`
	Label              string       `json:"label"`
	State              string       `json:"state"`
	Priority           int          `json:"priority"`
	AgingPerSec        float64      `json:"aging_per_sec"`
	TimeoutMS          int64        `json:"timeout_ms"`
	EffectivePriority  float64      `json:"effective_priority,omitempty"`
	QueuePosition      int          `json:"queue_position,omitempty"`
	PendingRequestID   *int64       `json:"pending_request_id,omitempty"`
	PendingRequestKind string       `json:"pending_request_kind,omitempty"`
	HeldResources      []Ref        `json:"held_resources"`
	WaitReasons        []WaitReason `json:"wait_reasons,omitempty"`
	Deadline           *time.Time   `json:"deadline,omitempty"`
	CreatedAt          time.Time    `json:"created_at"`
	StartedAt          *time.Time   `json:"started_at,omitempty"`
	UpdatedAt          time.Time    `json:"updated_at"`
}

// HoldView is evidence of who currently holds a resource.
type HoldView struct {
	Resource   Ref       `json:"resource"`
	TaskID     int64     `json:"task_id"`
	TaskLabel  string    `json:"task_label"`
	RequestID  int64     `json:"request_id"`
	AcquiredAt time.Time `json:"acquired_at"`
}

// EventView is a lifecycle event.
type EventView struct {
	ID     int64          `json:"id"`
	At     time.Time      `json:"at"`
	Event  string         `json:"event"`
	Detail map[string]any `json:"detail"`
}

// EvidenceView is one signed audit record.
type EvidenceView struct {
	ID        int64     `json:"id"`
	At        time.Time `json:"at"`
	Event     string    `json:"event"`
	TaskID    *int64    `json:"task_id"`
	RequestID *int64    `json:"request_id"`
	Resources []Ref     `json:"resources"`
	Canonical string    `json:"canonical"`
	Signature string    `json:"signature"`
	Valid     bool      `json:"valid"`
}

// Edge is one wait-for edge as returned by the debug endpoint.
type Edge struct {
	WaiterTaskID int64  `json:"waiter_task_id"`
	WaiterLabel  string `json:"waiter_label"`
	HolderTaskID int64  `json:"holder_task_id"`
	HolderLabel  string `json:"holder_label"`
	Resource     Ref    `json:"resource"`
}

// WaitGraph is the full dependency graph snapshot.
type WaitGraph struct {
	Edges  []Edge    `json:"edges"`
	Cycles [][]int64 `json:"cycles"`
}
