// Package scheduler implements a single-processor preemptive task simulator
// with mutexes and the priority inheritance protocol (PIP).
//
// The model is a discrete-event, tick-based simulation:
//
//   - Tasks arrive at a given tick, each with a base priority (larger number
//     means higher priority). They execute a Program: a list of instructions
//     (CPU work, lock acquire, lock release).
//   - At most one task runs on the (single) processor at any tick; higher
//     effective priority preempts the running task.
//   - Locks are exclusive. A task that cannot take a lock blocks and waits in
//     the lock's waiter queue (FIFO or priority ordered).
//   - With InheritancePIP, a lock owner that blocks a higher-priority waiter
//     inherits that priority; inheritance is transitive through nested lock
//     ownership and wait chains. Effective priorities are recomputed whenever
//     the wait/ownership graph changes, and restored when donors disappear.
//   - The blocking graph ("wait-for graph") is checked for cycles; every
//     distinct cycle is reported as a deadlock event.
//
// All state changes are emitted as structured Event records, giving a full
// scheduling timeline independent of the (replaceable) clock and executor.
package scheduler

// Op identifies a program instruction.
type Op string

const (
	OpCPU    Op = "cpu"    // run on the processor for N ticks
	OpLock   Op = "lock"   // acquire exclusive lock L (block if held by another)
	OpUnlock Op = "unlock" // release lock L
)

// Instr is one program instruction.
type Instr struct {
	Op    Op     `json:"op"`
	Lock  string `json:"lock,omitempty"`  // required for lock/unlock
	Ticks int    `json:"ticks,omitempty"` // required for cpu (must be > 0)
}

// Program is the ordered list of instructions a task executes.
type Program []Instr

// TaskSpec declares one task.
type TaskSpec struct {
	ID      string  `json:"id"`
	Base    int     `json:"basePriority"`
	Arrival int64   `json:"arrival"`
	Program Program `json:"program"`
}

// LockSpec declares one mutex. Only declared locks may be used.
type LockSpec struct {
	ID string `json:"id"`
}

// QueuePolicy selects how a lock's blocked waiters are woken.
type QueuePolicy string

const (
	// QueueFIFO wakes waiters in arrival (blocking) order — the classic
	// priority-inheritance setting, which preserves direct hand-off.
	QueueFIFO QueuePolicy = "fifo"
	// QueuePriority wakes the highest-effective-priority waiter first.
	QueuePriority QueuePolicy = "priority"
)

// InheritMode selects whether priority inheritance is enabled.
type InheritMode string

const (
	// InheritancePIP enables priority inheritance.
	InheritancePIP InheritMode = "pip"
	// InheritanceNone disables it: effective priority always equals base.
	// Used to reproduce classical unbounded priority inversion.
	InheritanceNone InheritMode = "none"
)

// Config configures a run.
type Config struct {
	Tasks       []TaskSpec  `json:"tasks"`
	Locks       []LockSpec  `json:"locks"`
	Inheritance InheritMode `json:"inheritance,omitempty"` // default pip
	QueuePolicy QueuePolicy `json:"queuePolicy,omitempty"` // default fifo
	MaxTicks    int64       `json:"maxTicks,omitempty"`    // safety bound; default 10000
	EmitCPUTick bool        `json:"emitCpuTick,omitempty"` // emit one event per executed CPU tick
}
