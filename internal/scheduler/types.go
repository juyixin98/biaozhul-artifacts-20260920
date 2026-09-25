// Package scheduler implements a single-processor, preemptive task scheduler
// with a mutex-lock simulator and the priority-inheritance protocol (Sha,
// Rajkumar & Lehoczky, 1990).
//
// Priority convention: the larger Priority is, the higher the priority.
// Ties are broken by specification order (earlier task wins), which keeps
// timelines deterministic.
//
// Time is discrete and measured in integer ticks; one tick is one unit of
// processor work. Lock acquire/release operations take zero time and are
// drained before the next tick is executed.
package scheduler

import (
	"fmt"
	"sort"

	"pim/internal/clock"
	"pim/internal/event"
	"pim/internal/program"
)

// TaskState is the lifecycle state of a task.
type TaskState string

const (
	// StWaiting: arrival time not reached yet.
	StWaiting TaskState = "waiting"
	// StReady: runnable but not currently on the processor.
	StReady TaskState = "ready"
	// StRunning: occupying the processor.
	StRunning TaskState = "running"
	// StBlocked: waiting to acquire a lock.
	StBlocked TaskState = "blocked"
	// StDone: program finished.
	StDone TaskState = "done"
)

// TaskSpec describes one task in a simulation request.
type TaskSpec struct {
	// ID is the unique task name (e.g. "High").
	ID string `json:"id"`
	// Arrival is the tick at which the task becomes ready.
	Arrival int64 `json:"arrival"`
	// Priority is the base priority; LARGER means higher priority.
	Priority int `json:"priority"`
	// Program is the fixed action script the task executes.
	Program *program.ScriptProgram `json:"program"`
}

// Options toggles protocol behaviour.
type Options struct {
	// PriorityInheritance enables the inheritance protocol. When false the
	// raw priority-inversion counter-example is produced.
	PriorityInheritance bool `json:"priorityInheritance"`
	// DeadlockDetection controls whether a wait-for cycle is reported as a
	// structured deadlock event (true, default). Either way the simulation
	// must terminate on a cycle, since no further progress is possible in a
	// closed model; when false, the run instead finishes with Result.Error
	// set and no deadlock event is emitted.
	DeadlockDetection *bool `json:"deadlockDetection,omitempty"`
}

// TaskExecution is the per-tick view handed to a replaceable Executor.
type TaskExecution struct {
	// TaskID identifies the running task.
	TaskID string
	// Tick is the logical time AFTER the tick completed.
	Tick int64
	// EffectivePriority is the priority the task ran at.
	EffectivePriority int
	// BurstLeft is the number of ticks remaining in the current CPU burst.
	BurstLeft int64
}

// Executor is the replaceable execution backend. The default
// (VirtualExecutor) performs no physical work — the model is fully
// discrete; alternative implementations can pace real time (see package
// pim/internal/executor), drive an external system, or collect metrics.
//
// Tick is invoked exactly once per processor tick, after the logical clock
// has advanced by one. Implementations must be safe to call sequentially;
// the simulation loop never invokes Tick concurrently.
type Executor interface {
	Tick(exec TaskExecution)
}

// VirtualExecutor is the no-op executor used by default: all physics live in
// the discrete clock.
type VirtualExecutor struct{}

// Tick implements Executor.
func (VirtualExecutor) Tick(TaskExecution) {}

// Config injects replaceable collaborators into RunWith. Nil fields receive
// defaults (virtual clock at 0, memory event sink, virtual executor).
type Config struct {
	Clock    clock.Clock
	Sink     event.Sink
	Executor Executor
}

// Spec is a complete simulation request.
type Spec struct {
	// Name is an optional human-readable label.
	Name string `json:"name,omitempty"`
	// Tasks lists the tasks in specification order (also the tie-break
	// order).
	Tasks []TaskSpec `json:"tasks"`
	// Resources declares the mutex lock IDs.
	Resources []string `json:"resources"`
	// Options configures the protocol.
	Options Options `json:"options"`
}

// Task is the runtime state of a task.
type Task struct {
	Spec       TaskSpec
	Index      int
	State      TaskState
	PC         int
	BurstLeft  int64
	Held       []*Lock
	WaitingFor *Lock
	EffPri     int
	Donors     []string
}

// ID returns the task's unique name.
func (t *Task) ID() string { return t.Spec.ID }

// Lock is the runtime state of a mutex.
type Lock struct {
	ID      string
	Owner   *Task
	Waiters []*Task // FIFO request order
}

// Result is the structured output of a run: the event timeline plus final
// state and summary statistics.
type Result struct {
	Name       string         `json:"name"`
	Options    Options        `json:"options"`
	FinishTime int64          `json:"finishTime"`
	Deadlock   *DeadlockInfo  `json:"deadlock,omitempty"`
	Error      string         `json:"error,omitempty"`
	Events     []event.Event  `json:"events"`
	Summary    Summary        `json:"summary"`
	Tasks      []TaskSnapshot `json:"tasks"`
	Locks      []LockSnapshot `json:"locks"`
}

// DeadlockInfo describes a detected wait-for cycle.
type DeadlockInfo struct {
	Cycle []string `json:"cycle"`
	At    int64    `json:"at"`
}

// Summary holds aggregate metrics of the run.
type Summary struct {
	MakespanTicks  int64            `json:"makespanTicks"`
	InversionTicks int64            `json:"inversionTicks"`
	BlockedTicks   map[string]int64 `json:"blockedTicks"`
	CompletedAt    map[string]int64 `json:"completedAt"`
}

// TaskSnapshot is the final state of a task in the result.
type TaskSnapshot struct {
	ID           string    `json:"id"`
	State        TaskState `json:"state"`
	BasePriority int       `json:"basePriority"`
	EffPriority  int       `json:"effectivePriority"`
	Donors       []string  `json:"donors,omitempty"`
	Held         []string  `json:"held,omitempty"`
	WaitingFor   string    `json:"waitingFor,omitempty"`
	Arrival      int64     `json:"arrival"`
	CompletedAt  int64     `json:"completedAt,omitempty"`
	BlockedTicks int64     `json:"blockedTicks"`
}

// LockSnapshot is the final state of a lock in the result.
type LockSnapshot struct {
	ID      string   `json:"id"`
	Owner   string   `json:"owner,omitempty"`
	Waiters []string `json:"waiters,omitempty"`
}

// state bundles everything the engine mutates.
type state struct {
	spec     Spec
	clk      clock.Clock
	sink     event.Sink
	executor Executor
	seq      int64
	tasks    []*Task
	byID     map[string]*Task
	locks    map[string]*Lock
	running  *Task

	inherit bool
	detect  bool

	blockedTicks map[string]int64
	completedAt  map[string]int64
	inversions   int64
	deadlock     *DeadlockInfo
	progErr      string
}

// WithDefaults fills default option values (deadlock detection defaults on).
func (s Spec) WithDefaults() Spec {
	if s.Options.DeadlockDetection == nil {
		on := true
		s.Options.DeadlockDetection = &on
	}
	return s
}

func (s Spec) validate() error {
	if len(s.Tasks) == 0 {
		return fmt.Errorf("spec: at least one task is required")
	}
	seenT := map[string]bool{}
	for i, t := range s.Tasks {
		if t.ID == "" {
			return fmt.Errorf("spec: task %d has empty id", i)
		}
		if seenT[t.ID] {
			return fmt.Errorf("spec: duplicate task id %q", t.ID)
		}
		seenT[t.ID] = true
		if t.Program == nil {
			return fmt.Errorf("spec: task %q has no program", t.ID)
		}
		if err := t.Program.Validate(); err != nil {
			return fmt.Errorf("spec: task %q: %w", t.ID, err)
		}
	}
	seenL := map[string]bool{}
	for _, l := range s.Resources {
		if l == "" {
			return fmt.Errorf("spec: empty resource id")
		}
		if seenT[l] {
			return fmt.Errorf("spec: resource id %q collides with a task id", l)
		}
		if seenL[l] {
			return fmt.Errorf("spec: duplicate resource id %q", l)
		}
		seenL[l] = true
	}
	// All lock references must resolve.
	for _, t := range s.Tasks {
		for _, a := range t.Program.Actions {
			if a.Acquire != "" && !seenL[a.Acquire] {
				return fmt.Errorf("spec: task %q acquires undeclared resource %q", t.ID, a.Acquire)
			}
			if a.Release != "" && !seenL[a.Release] {
				return fmt.Errorf("spec: task %q releases undeclared resource %q", t.ID, a.Release)
			}
		}
	}
	return nil
}

func newState(spec Spec, cfg Config) *state {
	st := &state{
		spec:         spec,
		clk:          cfg.Clock,
		sink:         cfg.Sink,
		executor:     cfg.Executor,
		byID:         map[string]*Task{},
		locks:        map[string]*Lock{},
		inherit:      spec.Options.PriorityInheritance,
		detect:       true,
		blockedTicks: map[string]int64{},
		completedAt:  map[string]int64{},
	}
	if spec.Options.DeadlockDetection != nil {
		st.detect = *spec.Options.DeadlockDetection
	}
	for i, ts := range spec.Tasks {
		t := &Task{Spec: ts, Index: i, State: StWaiting, EffPri: ts.Priority}
		st.tasks = append(st.tasks, t)
		st.byID[ts.ID] = t
	}
	for _, id := range spec.Resources {
		st.locks[id] = &Lock{ID: id}
	}
	return st
}

func (st *state) emit(k event.Kind, task, resource string, detail map[string]any) {
	st.sink.Emit(event.Event{
		Seq:      st.seq,
		Time:     st.clk.Now(),
		Kind:     k,
		Task:     task,
		Resource: resource,
		Detail:   detail,
	})
	st.seq++
}

// donorSets computes, for every task, the set of tasks whose blocking chain
// passes through one of its held locks (transitive donation).
func (st *state) donorSets() map[string]map[string]*Task {
	donors := map[string]map[string]*Task{}
	for _, b := range st.tasks {
		if b.State != StBlocked || b.WaitingFor == nil {
			continue
		}
		visited := map[string]bool{b.ID(): true}
		cur := b
		for {
			lk := cur.WaitingFor
			if lk == nil || lk.Owner == nil {
				break
			}
			owner := lk.Owner
			if owner != b { // a wait-for cycle never implies self-donation
				if donors[owner.ID()] == nil {
					donors[owner.ID()] = map[string]*Task{}
				}
				donors[owner.ID()][b.ID()] = b
			}
			if visited[owner.ID()] {
				break // wait-for cycle: deadlock pass handles reporting
			}
			visited[owner.ID()] = true
			cur = owner
			if cur.State != StBlocked {
				break
			}
		}
	}
	return donors
}

// recomputePriorities finds the fixed point eff(T) = max(base(T),
// max eff(D) over donors D) and emits priority_change events for every task
// whose effective priority moved.
func (st *state) recomputePriories(reason string) {
	donors := st.donorSets()

	eff := map[string]int{}
	for _, t := range st.tasks {
		eff[t.ID()] = t.Spec.Priority
	}
	// Monotone fixed point: values only rise; a handful of passes suffice,
	// but iterate up to a safe bound derived from the task count.
	for changed := true; changed; {
		changed = false
		for _, t := range st.tasks {
			best := t.Spec.Priority
			for _, d := range donors[t.ID()] {
				if eff[d.ID()] > best {
					best = eff[d.ID()]
				}
			}
			if best != eff[t.ID()] {
				eff[t.ID()] = best
				changed = true
			}
		}
	}

	for _, t := range st.tasks {
		newEff := t.Spec.Priority
		donorList := []string{}
		if st.inherit {
			newEff = eff[t.ID()]
			for id := range donors[t.ID()] {
				donorList = append(donorList, id)
			}
			sort.Strings(donorList)
		}
		oldEff := t.EffPri
		t.EffPri = newEff
		t.Donors = donorList
		if oldEff != newEff {
			why := reason
			if why == "" {
				if newEff > t.Spec.Priority {
					why = "inheritance"
				} else {
					why = "restore"
				}
			}
			st.emit(event.PriorityChange, t.ID(), "", map[string]any{
				"base":          t.Spec.Priority,
				"old":           oldEff,
				"new":           newEff,
				"donors":        donorList,
				"reason":        why,
				"inheritanceOn": st.inherit,
			})
		}
	}
}
