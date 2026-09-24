// Package sim implements a tick-based discrete-event simulation of an
// offline cluster scheduler with checkpointing and priority preemption.
//
// Rules of the model:
//
//   - Time advances in integer ticks. Every tick, each running task executes
//     one unit of work; each saving task spends one tick writing a checkpoint.
//   - A task occupies `resource_demand` units of cluster capacity while
//     running AND while saving a checkpoint. Checkpoint save time is real
//     overhead and the save holds the resources.
//   - After every `checkpoint_every` units of work the task starts a
//     `checkpoint_cost`-tick checkpoint save. The checkpoint is only usable
//     AFTER the save fully completes ("commits"). A save interrupted by
//     preemption is discarded: its target was never a committed checkpoint.
//   - In preemptive mode a higher-priority task that cannot fit may evict
//     strictly-lower-priority active tasks. An evicted task keeps its last
//     committed checkpoint; all work after it is repeated when it resumes.
//     Tasks of equal priority never evict each other.
//   - In non-preemptive mode running/saving tasks are never evicted; ready
//     tasks wait for capacity in priority order.
package sim

import (
	"fmt"
	"sort"
)

// Policy selects the scheduling strategy simulated.
type Policy string

const (
	// PolicyPreemptive allows higher-priority tasks to evict lower-priority ones.
	PolicyPreemptive Policy = "preemptive"
	// PolicyNonPreemptive never evicts an active task.
	PolicyNonPreemptive Policy = "non_preemptive"
)

// TaskSpec is the static description of one job.
type TaskSpec struct {
	// ID must be unique and non-empty.
	ID string `json:"id"`
	// Priority: larger value = higher priority.
	Priority int `json:"priority"`
	// ArrivalTick is the tick at/after which the task is available to schedule.
	ArrivalTick int64 `json:"arrival_tick"`
	// TotalWork is the total execution units required (excluding checkpoint saves).
	TotalWork int64 `json:"total_work"`
	// CheckpointEvery: a checkpoint is started after this many units of NEW work
	// since the last committed checkpoint.
	CheckpointEvery int64 `json:"checkpoint_every"`
	// CheckpointCost is the number of ticks a checkpoint save takes (and holds resources).
	CheckpointCost int64 `json:"checkpoint_cost"`
	// ResourceDemand units of cluster capacity occupied while running/saving.
	ResourceDemand int `json:"resource_demand"`
}

// Event is one entry in the simulated timeline.
type Event struct {
	Tick   int64  `json:"tick"`
	Type   string `json:"type"`
	TaskID string `json:"task_id,omitempty"`
	// FromCheckpoint is present on resume events: the committed checkpoint
	// work position the task resumes from.
	FromCheckpoint int64  `json:"from_checkpoint,omitempty"`
	Detail         string `json:"detail,omitempty"`
}

// Event type constants.
const (
	EvArrive     = "arrive"
	EvStart      = "start"
	EvResume     = "resume"
	EvSaveBegin  = "save_begin"
	EvCheckpoint = "checkpoint_committed"
	EvPreempt    = "preempt"
	EvComplete   = "complete"
)

// TaskResult is the per-task outcome of one policy run.
type TaskResult struct {
	ID                   string `json:"id"`
	Priority             int    `json:"priority"`
	ArrivalTick          int64  `json:"arrival_tick"`
	CompletionTick       int64  `json:"completion_tick"` // -1 if not completed
	WaitTicks            int64  `json:"wait_ticks"`
	RedundantWork        int64  `json:"redundant_work"`
	WastedSaveTicks      int64  `json:"wasted_save_ticks"`
	CompletedCheckpoints int    `json:"completed_checkpoints"`
	PreemptedCount       int    `json:"preempted_count"`
	Completed            bool   `json:"completed"`
}

// Totals aggregate accounting over one policy run.
type Totals struct {
	WorkTicks            int64 `json:"work_ticks"`
	SaveTicks            int64 `json:"save_ticks"`
	WastedSaveTicks      int64 `json:"wasted_save_ticks"`
	RedundantWork        int64 `json:"redundant_work"`
	CompletedCheckpoints int   `json:"completed_checkpoints"`
	Preemptions          int   `json:"preemptions"`
}

// RunResult is the output of simulating one policy.
type RunResult struct {
	Policy         Policy       `json:"policy"`
	Makespan       int64        `json:"makespan"` // tick at which all tasks finished; -1 if safety cap hit
	AllCompleted   bool         `json:"all_completed"`
	TicksSimulated int64        `json:"ticks_simulated"`
	Tasks          []TaskResult `json:"tasks"`
	Totals         Totals       `json:"totals"`
	Timeline       []Event      `json:"timeline"`
}

// TaskComparison compares completion ticks for one task between policies.
type TaskComparison struct {
	ID                      string `json:"id"`
	Priority                int    `json:"priority"`
	ArrivalTick             int64  `json:"arrival_tick"`
	PreemptiveCompletion    int64  `json:"preemptive_completion"` // -1 if not completed
	NonPreemptiveCompletion int64  `json:"non_preemptive_completion"`
	// DeltaTicks = non_preemptive - preemptive completion; positive means
	// the task finished earlier under preemption.
	DeltaTicks int64 `json:"delta_ticks"`
}

// Result holds both policy runs and the comparison.
type Result struct {
	Capacity       int              `json:"capacity"`
	Preemptive     RunResult        `json:"preemptive"`
	NonPreemptive  RunResult        `json:"non_preemptive"`
	MakespanDelta  int64            `json:"makespan_delta"` // non_preemptive - preemptive; positive favors preemption
	TaskComparison []TaskComparison `json:"task_comparison"`
}

// Validate checks the request and returns a descriptive error on bad input.
func Validate(capacity int, tasks []TaskSpec) error {
	if capacity < 1 {
		return fmt.Errorf("capacity must be >= 1, got %d", capacity)
	}
	seen := map[string]bool{}
	for i, t := range tasks {
		if t.ID == "" {
			return fmt.Errorf("tasks[%d]: id must be non-empty", i)
		}
		if seen[t.ID] {
			return fmt.Errorf("tasks[%d]: duplicate id %q", i, t.ID)
		}
		seen[t.ID] = true
		if t.ArrivalTick < 0 {
			return fmt.Errorf("task %q: arrival_tick must be >= 0", t.ID)
		}
		if t.TotalWork < 1 {
			return fmt.Errorf("task %q: total_work must be >= 1", t.ID)
		}
		if t.CheckpointEvery < 1 {
			return fmt.Errorf("task %q: checkpoint_every must be >= 1", t.ID)
		}
		if t.CheckpointCost < 0 {
			return fmt.Errorf("task %q: checkpoint_cost must be >= 0", t.ID)
		}
		if t.ResourceDemand < 1 {
			return fmt.Errorf("task %q: resource_demand must be >= 1", t.ID)
		}
		if t.ResourceDemand > capacity {
			return fmt.Errorf("task %q: resource_demand %d exceeds cluster capacity %d", t.ID, t.ResourceDemand, capacity)
		}
	}
	return nil
}

// Simulate validates the input and runs BOTH policies over the identical
// scenario, returning a comparison of completion times.
func Simulate(capacity int, tasks []TaskSpec) (Result, error) {
	if err := Validate(capacity, tasks); err != nil {
		return Result{}, err
	}
	capTicks := safetyCap(tasks)
	res := Result{Capacity: capacity}
	res.Preemptive = runPolicy(tasks, capacity, PolicyPreemptive, capTicks)
	res.NonPreemptive = runPolicy(tasks, capacity, PolicyNonPreemptive, capTicks)
	if res.Preemptive.AllCompleted && res.NonPreemptive.AllCompleted {
		res.MakespanDelta = res.NonPreemptive.Makespan - res.Preemptive.Makespan
	} else {
		res.MakespanDelta = 0
	}

	pc := map[string]TaskResult{}
	for _, tr := range res.Preemptive.Tasks {
		pc[tr.ID] = tr
	}
	res.TaskComparison = make([]TaskComparison, 0, len(tasks))
	for _, ntr := range res.NonPreemptive.Tasks {
		ptr := pc[ntr.ID]
		var delta int64
		if ntr.Completed && ptr.Completed {
			delta = ntr.CompletionTick - ptr.CompletionTick
		}
		res.TaskComparison = append(res.TaskComparison, TaskComparison{
			ID:                      ntr.ID,
			Priority:                ntr.Priority,
			ArrivalTick:             ntr.ArrivalTick,
			PreemptiveCompletion:    ptr.CompletionTick,
			NonPreemptiveCompletion: ntr.CompletionTick,
			DeltaTicks:              delta,
		})
	}
	return res, nil
}

func safetyCap(tasks []TaskSpec) int64 {
	var sum int64
	var maxArrival int64
	for _, t := range tasks {
		// Upper bound per task: all work plus one save per work unit is
		// absurdly pessimistic but always safe.
		sum += t.TotalWork * (1 + t.CheckpointCost)
		if t.ArrivalTick > maxArrival {
			maxArrival = t.ArrivalTick
		}
	}
	c := maxArrival + sum*int64(len(tasks)+1) + 1024
	if c < 10000 {
		c = 10000
	}
	return c
}

// ---------------------------------------------------------------------------
// Internal engine
// ---------------------------------------------------------------------------

type taskState int

const (
	stUnarrived taskState = iota
	stReady
	stRunning
	stSaving
	stDone
)

type task struct {
	spec TaskSpec

	state       taskState
	progress    int64 // new units of work executed since the committed checkpoint
	committed   int64 // work position of the last committed checkpoint
	saveRemain  int64 // ticks left for the in-flight checkpoint save
	saveSpent   int64 // ticks already spent on the in-flight save
	allocatedAt int64 // tick at which current resource allocation began
	startedAt   int64 // first ever start, -1 if never
	completedAt int64 // -1 if not done
	waitTicks   int64
	redundant   int64
	wastedSave  int64
	cpCommitted int
	preemptions int
}

func newTask(s TaskSpec) *task {
	return &task{spec: s, startedAt: -1, completedAt: -1}
}

func (t *task) demand() int { return t.spec.ResourceDemand }

func runPolicy(specs []TaskSpec, capacity int, policy Policy, capTicks int64) RunResult {
	tasks := make([]*task, 0, len(specs))
	for _, s := range specs {
		tasks = append(tasks, newTask(s))
	}

	used := 0
	var totals Totals
	var timeline []Event
	emit := func(tick int64, typ, id string, fromCP int64, detail string) {
		timeline = append(timeline, Event{Tick: tick, Type: typ, TaskID: id, FromCheckpoint: fromCP, Detail: detail})
	}

	tick := int64(0)
	for {
		// 1. Admissions.
		for _, t := range tasks {
			if t.state == stUnarrived && t.spec.ArrivalTick <= tick {
				t.state = stReady
				emit(tick, EvArrive, t.spec.ID, 0, "")
			}
		}

		// 2. Scheduling. Restart the priority scan after every allocation so
		//    that tasks just released by preemption re-enter the ordering.
		for {
			ready := sortedReady(tasks)
			madeProgress := false
			for _, cand := range ready {
				if used+cand.demand() <= capacity {
					allocate(cand, tick, capacity, &used, emit)
					madeProgress = true
					break
				}
				if policy == PolicyPreemptive {
					victims := chooseVictims(cand, tasks, used+cand.demand()-capacity)
					if victims != nil {
						for _, v := range victims {
							evict(v, tick, &used, &totals, emit)
						}
						allocate(cand, tick, capacity, &used, emit)
						madeProgress = true
						break
					}
				}
				// Doesn't fit and (for this policy) cannot make room: leave
				// ready. Keep scanning — a later, smaller task may still fit.
			}
			if !madeProgress {
				break
			}
		}

		// 3. Tasks still ready after scheduling accrue one tick of waiting.
		for _, t := range tasks {
			if t.state == stReady {
				t.waitTicks++
			}
		}

		// 4. Execute one tick for every active task.
		for _, t := range tasks {
			switch t.state {
			case stRunning:
				t.progress++
				totals.WorkTicks++
				if t.committed+t.progress >= t.spec.TotalWork {
					t.state = stDone
					t.completedAt = tick + 1
					used -= t.demand()
					emit(tick+1, EvComplete, t.spec.ID, 0, "")
					continue
				}
				if t.progress >= t.spec.CheckpointEvery {
					if t.spec.CheckpointCost == 0 {
						// Zero-cost save: commits immediately, still observable
						// as a committed checkpoint.
						t.committed += t.progress
						t.progress = 0
						t.cpCommitted++
						totals.CompletedCheckpoints++
						emit(tick, EvCheckpoint, t.spec.ID, t.committed, "")
					} else {
						t.state = stSaving
						t.saveRemain = t.spec.CheckpointCost
						t.saveSpent = 0
						emit(tick, EvSaveBegin, t.spec.ID, t.committed,
							fmt.Sprintf("target=%d", t.committed+t.progress))
					}
				}
			case stSaving:
				t.saveRemain--
				t.saveSpent++
				totals.SaveTicks++
				if t.saveRemain == 0 {
					// Checkpoint fully written: only NOW is it usable.
					t.committed += t.progress
					t.progress = 0
					t.cpCommitted++
					totals.CompletedCheckpoints++
					t.state = stRunning
					emit(tick+1, EvCheckpoint, t.spec.ID, t.committed, "")
				}
			}
		}

		// 5. Termination.
		allDone := true
		for _, t := range tasks {
			if t.state != stDone {
				allDone = false
				break
			}
		}
		if allDone {
			return buildResult(policy, tasks, totals, timeline, tick+1, true)
		}
		tick++
		if tick >= capTicks {
			return buildResult(policy, tasks, totals, timeline, tick, false)
		}
	}
}

func sortedReady(tasks []*task) []*task {
	var ready []*task
	for _, t := range tasks {
		if t.state == stReady {
			ready = append(ready, t)
		}
	}
	sort.SliceStable(ready, func(i, j int) bool {
		a, b := ready[i], ready[j]
		if a.spec.Priority != b.spec.Priority {
			return a.spec.Priority > b.spec.Priority
		}
		if a.spec.ArrivalTick != b.spec.ArrivalTick {
			return a.spec.ArrivalTick < b.spec.ArrivalTick
		}
		return a.spec.ID < b.spec.ID
	})
	return ready
}

// chooseVictims returns the cheapest (by the ordering below) set of strictly
// lower-priority active tasks whose freed capacity covers `need`. It returns
// nil instead of a partial set so preemption never happens unless the
// candidate can actually start.
func chooseVictims(cand *task, tasks []*task, need int) []*task {
	var eligible []*task
	for _, t := range tasks {
		if t == cand {
			continue
		}
		if (t.state == stRunning || t.state == stSaving) && t.spec.Priority < cand.spec.Priority {
			eligible = append(eligible, t)
		}
	}
	// Lowest priority first; among equal priority the longest-allocated task
	// (it holds the most sunk checkpoint cost advantage only indirectly; this
	// tie-break is simply deterministic), then ID.
	sort.SliceStable(eligible, func(i, j int) bool {
		a, b := eligible[i], eligible[j]
		if a.spec.Priority != b.spec.Priority {
			return a.spec.Priority < b.spec.Priority
		}
		if a.allocatedAt != b.allocatedAt {
			return a.allocatedAt < b.allocatedAt
		}
		return a.spec.ID < b.spec.ID
	})
	var picked []*task
	freed := 0
	for _, t := range eligible {
		picked = append(picked, t)
		freed += t.demand()
		if freed >= need {
			return picked
		}
	}
	return nil
}

func allocate(t *task, tick int64, capacity int, used *int, emit func(int64, string, string, int64, string)) {
	*used += t.demand()
	t.allocatedAt = tick
	if t.startedAt == -1 {
		t.startedAt = tick
		t.state = stRunning
		emit(tick, EvStart, t.spec.ID, 0, "")
	} else {
		t.state = stRunning
		emit(tick, EvResume, t.spec.ID, t.committed, "")
	}
}

func evict(t *task, tick int64, used *int, totals *Totals, emit func(int64, string, string, int64, string)) {
	*used -= t.demand()
	wasSaving := t.state == stSaving
	t.state = stReady
	t.preemptions++
	totals.Preemptions++
	detail := ""
	if wasSaving {
		// Interrupted checkpoint save: discarded wholesale.
		t.wastedSave += t.saveSpent
		totals.WastedSaveTicks += t.saveSpent
		// Work since the last committed checkpoint must be redone.
		t.redundant += t.progress
		totals.RedundantWork += t.progress
		detail = fmt.Sprintf("in_flight_checkpoint_discarded: wasted_save_ticks=%d resume_from=%d",
			t.saveSpent, t.committed)
		t.saveRemain, t.saveSpent = 0, 0
		t.progress = 0
	} else {
		// Evicted while running: un-checkpointed progress is lost.
		t.redundant += t.progress
		totals.RedundantWork += t.progress
		if t.progress > 0 {
			detail = fmt.Sprintf("resume_from=%d", t.committed)
		}
		t.progress = 0
	}
	emit(tick, EvPreempt, t.spec.ID, t.committed, detail)
}

func buildResult(policy Policy, tasks []*task, totals Totals, timeline []Event, makespan int64, allDone bool) RunResult {
	results := make([]TaskResult, 0, len(tasks))
	ordered := make([]*task, len(tasks))
	copy(ordered, tasks)
	sort.SliceStable(ordered, func(i, j int) bool { return ordered[i].spec.ID < ordered[j].spec.ID })
	for _, t := range ordered {
		results = append(results, TaskResult{
			ID:                   t.spec.ID,
			Priority:             t.spec.Priority,
			ArrivalTick:          t.spec.ArrivalTick,
			CompletionTick:       t.completedAt,
			WaitTicks:            t.waitTicks,
			RedundantWork:        t.redundant,
			WastedSaveTicks:      t.wastedSave,
			CompletedCheckpoints: t.cpCommitted,
			PreemptedCount:       t.preemptions,
			Completed:            t.state == stDone,
		})
	}
	if !allDone {
		makespan = -1
	}
	return RunResult{
		Policy:       policy,
		Makespan:     makespan,
		AllCompleted: allDone,
		TicksSimulated: func() int64 {
			var maxTick int64
			for _, e := range timeline {
				if e.Tick > maxTick {
					maxTick = e.Tick
				}
			}
			return maxTick
		}(),
		Tasks:    results,
		Totals:   totals,
		Timeline: timeline,
	}
}
