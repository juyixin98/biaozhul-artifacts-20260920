// Package sim implements a deterministic discrete-time real-time scheduler
// simulator with optional priority inheritance (PIP).
//
// Time is integer ("ticks"). A larger BasePriority value means a HIGHER
// scheduling priority (e.g. 40 > 10). Tasks are described as sequences of
// zero-duration lock/unlock operations separated by compute segments of an
// integer number of ticks.
package sim

// OpKind enumerates the operations a task can perform.
type OpKind string

const (
	// OpCompute runs the CPU for Duration ticks.
	OpCompute OpKind = "compute"
	// OpLock attempts to acquire the named mutex resource (zero duration).
	OpLock OpKind = "lock"
	// OpUnlock releases the named mutex resource (zero duration).
	OpUnlock OpKind = "unlock"
)

// Op is one element of a task program.
type Op struct {
	Kind     OpKind `json:"kind"`
	Resource string `json:"resource,omitempty"`
	Duration int    `json:"duration,omitempty"` // compute only, in ticks
}

// TaskSpec is a static task definition.
type TaskSpec struct {
	ID           string `json:"id"`
	BasePriority int    `json:"base_priority"` // larger value == higher priority
	Release      int    `json:"release"`       // first ready tick
	Ops          []Op   `json:"ops"`
}

// Workload is a complete simulation input.
type Workload struct {
	Name      string     `json:"name,omitempty"`
	Resources []string   `json:"resources"`
	Tasks     []TaskSpec `json:"tasks"`
	// MaxTicks is a safety bound on simulated time. Defaults to DefaultMaxTicks.
	MaxTicks int `json:"max_ticks,omitempty"`
}

// DefaultMaxTicks bounds a single simulation run.
const DefaultMaxTicks = 100_000

// Event is one entry in the deterministic decision trace.
type Event struct {
	Tick      int               `json:"tick"`
	Type      string            `json:"type"`
	Task      string            `json:"task,omitempty"`
	Resource  string            `json:"resource,omitempty"`
	GrantedTo string            `json:"granted_to,omitempty"`
	OpIndex   *int              `json:"op_index,omitempty"`
	BasePrio  *int              `json:"base_priority,omitempty"`
	EffPrio   *int              `json:"eff_priority,omitempty"`
	FromPrio  *int              `json:"from_priority,omitempty"`
	ToPrio    *int              `json:"to_priority,omitempty"`
	Remaining *int              `json:"remaining,omitempty"`
	Selected  string            `json:"selected,omitempty"`
	Ready     []string          `json:"ready,omitempty"`
	Blocked   map[string]string `json:"blocked,omitempty"` // task id -> resource waited on
	Reason    string            `json:"reason,omitempty"`
	FromTick  *int              `json:"from_tick,omitempty"` // idle jumps
	NextTick  *int              `json:"next_tick,omitempty"`
	Cycle     []string          `json:"cycle,omitempty"` // deadlock
}

// TaskMetrics summarizes one task's outcome.
type TaskMetrics struct {
	ID            string `json:"id"`
	Release       int    `json:"release"`
	StartedAt     int    `json:"started_at"`    // -1 if never scheduled
	FinishedAt    int    `json:"finished_at"`   // -1 if not finished
	ResponseTime  int    `json:"response_time"` // finished_at - release; -1 if unfinished
	ExecutedTicks int    `json:"executed_ticks"`
	BlockedTicks  int    `json:"blocked_ticks"` // ticks spent waiting on a mutex
	BlockedCount  int    `json:"blocked_count"` // number of blocking episodes
}

// Result is the output of one simulation run.
type Result struct {
	Inheritance     bool                   `json:"inheritance"`
	Ticks           int                    `json:"ticks"`
	CompletionOrder []string               `json:"completion_order"`
	Metrics         map[string]TaskMetrics `json:"metrics"`
	Deadlocked      bool                   `json:"deadlocked"`
	DeadlockCycle   []string               `json:"deadlock_cycle,omitempty"`
	Events          []Event                `json:"events"`
}

// TaskComparison compares one task between the two scheduler modes.
type TaskComparison struct {
	ID                 string `json:"id"`
	BlockedWithoutPIP  int    `json:"blocked_without_pip"`
	BlockedWithPIP     int    `json:"blocked_with_pip"`
	BlockedDelta       int    `json:"blocked_delta"` // without - with (positive == PIP helped)
	StartedWithoutPIP  int    `json:"started_without_pip"`
	StartedWithPIP     int    `json:"started_with_pip"`
	FinishedWithoutPIP int    `json:"finished_without_pip"`
	FinishedWithPIP    int    `json:"finished_with_pip"`
}

// ComparisonReport runs the same workload with and without PIP.
type ComparisonReport struct {
	Workload               Workload         `json:"workload"`
	WithoutInheritance     *Result          `json:"without_inheritance"`
	WithInheritance        *Result          `json:"with_inheritance"`
	Tasks                  []TaskComparison `json:"tasks"`
	TotalBlockedWithoutPIP int              `json:"total_blocked_without_pip"`
	TotalBlockedWithPIP    int              `json:"total_blocked_with_pip"`
	BlockedDelta           int              `json:"blocked_delta"`
}
