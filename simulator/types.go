// Package simulator implements a deterministic, discrete-time real-time
// scheduling simulator with the Priority Inheritance Protocol (PIP).
//
// Time is integer: an event at time t describes the decision made for the
// half-open tick [t, t+1). Lock/unlock operations are instantaneous and are
// serviced at the start of a tick before compute consumes the tick.
package simulator

// Op is a single operation inside a task's fixed execution sequence.
type Op string

const (
	// OpCompute executes on the CPU for Duration ticks.
	OpCompute Op = "compute"
	// OpLock acquires the named mutex resource (blocking until available).
	OpLock Op = "lock"
	// OpUnlock releases the named mutex resource.
	OpUnlock Op = "unlock"
)

// Step is one element of a task program.
type Step struct {
	Op       Op     `json:"op"`
	Resource string `json:"resource,omitempty"` // used by lock / unlock
	Duration int    `json:"duration,omitempty"` // used by compute, unit: ticks
}

// TaskSpec is a static task definition. Higher Priority values mean more
// urgent (higher priority) tasks. ReleaseTime is the first tick at which the
// task may run.
type TaskSpec struct {
	Name        string `json:"name"`
	Priority    int    `json:"priority"`
	ReleaseTime int    `json:"releaseTime"`
	Steps       []Step `json:"steps"`
}

// Config is the simulator input.
type Config struct {
	// EnableInheritance selects PIP (true) or plain fixed-priority
	// preemptive scheduling without priority inheritance (false).
	EnableInheritance bool       `json:"enableInheritance"`
	Tasks             []TaskSpec `json:"tasks"`
	// MaxTime bounds a run as a safety net (deadlock, buggy input).
	// 0 selects the default (100000 ticks).
	MaxTime int `json:"maxTime,omitempty"`
}

// CompareRequest runs the identical workload twice, with and without PIP.
type CompareRequest struct {
	Tasks   []TaskSpec `json:"tasks"`
	MaxTime int        `json:"maxTime,omitempty"`
}
