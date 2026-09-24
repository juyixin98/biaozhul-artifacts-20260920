package main

import (
	"fmt"
	"math"
	"sort"
)

// TaskSpec describes one job submitted to the simulated cluster.
//
// Time is modeled in abstract units (float64). A task alternates between
// working and checkpointing: after every CheckpointInterval units of work it
// spends CheckpointCost time saving a checkpoint. While saving, the task
// still occupies its cluster slot but makes no progress.
type TaskSpec struct {
	ID                 string  `json:"id"`
	Priority           int     `json:"priority"` // higher = more important
	Arrival            float64 `json:"arrival"`
	Work               float64 `json:"work"`                // total work units required
	CheckpointInterval float64 `json:"checkpoint_interval"` // work units between checkpoints
	CheckpointCost     float64 `json:"checkpoint_cost"`     // time units spent per checkpoint save
}

// Event is one line of the simulation trace.
type Event struct {
	Time   float64 `json:"time"`
	Task   string  `json:"task"`
	Kind   string  `json:"kind"` // arrival, start, checkpoint_begin, checkpoint_commit, checkpoint_aborted, preempted, resume, complete
	Detail string  `json:"detail,omitempty"`
}

// TaskResult summarizes the simulated lifecycle of one task.
type TaskResult struct {
	ID                   string  `json:"id"`
	Priority             int     `json:"priority"`
	Arrival              float64 `json:"arrival"`
	Completion           float64 `json:"completion"`
	Turnaround           float64 `json:"turnaround"` // completion - arrival
	CheckpointsCommitted int     `json:"checkpoints_committed"`
	CheckpointsAborted   int     `json:"checkpoints_aborted"` // saves interrupted before completion (never usable)
	Preemptions          int     `json:"preemptions"`
	WorkLost             float64 `json:"work_lost"` // uncheckpointed work discarded by preemption
}

// SimResult is the outcome of one simulation run.
type SimResult struct {
	PreemptionEnabled bool         `json:"preemption_enabled"`
	Slots             int          `json:"slots"`
	Events            []Event      `json:"events"`
	Tasks             []TaskResult `json:"tasks"`
	Makespan          float64      `json:"makespan"`
}

// CompareResult runs the same scenario with and without preemption.
type CompareResult struct {
	WithPreemption    SimResult          `json:"with_preemption"`
	WithoutPreemption SimResult          `json:"without_preemption"`
	CompletionDelta   map[string]float64 `json:"completion_delta"` // per task: without - with (positive = preemption helped)
}

// SimRequest is the body of POST /api/simulate and POST /api/compare.
type SimRequest struct {
	Slots   int        `json:"slots"`
	Preempt *bool      `json:"preempt,omitempty"` // nil defaults to true; ignored by /api/compare
	Tasks   []TaskSpec `json:"tasks"`
}

type phase int

const (
	phaseWorking phase = iota
	phaseSaving
)

type taskState struct {
	spec          TaskSpec
	arrived       bool
	done          bool
	running       bool
	secured       float64 // work committed by completed checkpoints
	segDone       float64 // work done since last committed checkpoint
	phase         phase
	saveRemaining float64
	completion    float64
	committed     int
	aborted       int
	preemptions   int
	workLost      float64
}

func validate(req *SimRequest) error {
	if req.Slots < 1 {
		return fmt.Errorf("slots must be >= 1")
	}
	if len(req.Tasks) == 0 {
		return fmt.Errorf("at least one task is required")
	}
	seen := map[string]bool{}
	for _, t := range req.Tasks {
		if t.ID == "" {
			return fmt.Errorf("task id must not be empty")
		}
		if seen[t.ID] {
			return fmt.Errorf("duplicate task id %q", t.ID)
		}
		seen[t.ID] = true
		if t.Arrival < 0 {
			return fmt.Errorf("task %q: arrival must be >= 0", t.ID)
		}
		if t.Work <= 0 {
			return fmt.Errorf("task %q: work must be > 0", t.ID)
		}
		if t.CheckpointInterval <= 0 {
			return fmt.Errorf("task %q: checkpoint_interval must be > 0", t.ID)
		}
		if t.CheckpointCost < 0 {
			return fmt.Errorf("task %q: checkpoint_cost must be >= 0", t.ID)
		}
	}
	return nil
}

const eps = 1e-9

// simulate runs an event-driven simulation of the cluster.
//
// Scheduling rule: free slots go to the highest-priority waiting tasks
// (ties: earlier arrival, then id). When preemption is enabled and a waiting
// task outranks a running one, the lowest-priority running task is preempted.
// A preempted task loses ALL work since its last *committed* checkpoint; an
// in-progress checkpoint save is aborted and can never be resumed from.
func simulate(req SimRequest, preempt bool) (SimResult, error) {
	if err := validate(&req); err != nil {
		return SimResult{}, err
	}

	states := make([]*taskState, len(req.Tasks))
	for i, t := range req.Tasks {
		states[i] = &taskState{spec: t}
	}
	// Deterministic processing order for equal timestamps.
	sort.SliceStable(states, func(i, j int) bool {
		if states[i].spec.Arrival != states[j].spec.Arrival {
			return states[i].spec.Arrival < states[j].spec.Arrival
		}
		return states[i].spec.ID < states[j].spec.ID
	})

	res := SimResult{PreemptionEnabled: preempt, Slots: req.Slots}
	now := 0.0
	log := func(task, kind, detail string) {
		res.Events = append(res.Events, Event{Time: round(now), Task: task, Kind: kind, Detail: detail})
	}

	remaining := len(states)
	for remaining > 0 {
		// 1. Advance clock to the next event: next arrival or next phase end.
		next := math.Inf(1)
		for _, s := range states {
			if !s.arrived && s.spec.Arrival < next {
				next = s.spec.Arrival
			}
			if s.running {
				end := now + s.phaseTimeLeft()
				if end < next {
					next = end
				}
			}
		}
		if math.IsInf(next, 1) {
			break // nothing left to wait for (should not happen)
		}
		dt := next - now
		now = next

		// 2. Apply elapsed time to running tasks.
		for _, s := range states {
			if !s.running {
				continue
			}
			if s.phase == phaseWorking {
				s.segDone += dt
			} else {
				s.saveRemaining -= dt
			}
		}

		// 3. Resolve phase transitions at the current instant (may cascade:
		// a zero-cost checkpoint completes immediately after it begins).
		for {
			changed := false
			for _, s := range states {
				if !s.running || s.done {
					continue
				}
				switch s.phase {
				case phaseWorking:
					total := s.secured + s.segDone
					if total >= s.spec.Work-eps {
						s.running = false
						s.done = true
						s.completion = now
						remaining--
						log(s.spec.ID, "complete", fmt.Sprintf("finished %.4g units of work", s.spec.Work))
						changed = true
					} else if s.segDone >= s.spec.CheckpointInterval-eps {
						s.phase = phaseSaving
						s.saveRemaining = s.spec.CheckpointCost
						log(s.spec.ID, "checkpoint_begin", fmt.Sprintf("saving checkpoint at %.4g/%.4g work units", total, s.spec.Work))
						changed = true
					}
				case phaseSaving:
					if s.saveRemaining <= eps {
						s.secured += s.segDone
						s.segDone = 0
						s.phase = phaseWorking
						s.committed++
						log(s.spec.ID, "checkpoint_commit", fmt.Sprintf("checkpoint committed, %.4g/%.4g work units secured", s.secured, s.spec.Work))
						changed = true
					}
				}
			}
			if !changed {
				break
			}
		}

		// 4. Process arrivals at this instant.
		for _, s := range states {
			if !s.arrived && s.spec.Arrival <= now+eps {
				s.arrived = true
				log(s.spec.ID, "arrival", fmt.Sprintf("priority %d, %.4g work units", s.spec.Priority, s.spec.Work))
			}
		}

		// 5. Scheduling pass: preemption first, then fill free slots.
		if preempt {
			for countRunning(states) >= req.Slots {
				victim := lowestPriorityRunning(states)
				waiter := highestPriorityWaiting(states)
				if victim == nil || waiter == nil || waiter.spec.Priority <= victim.spec.Priority {
					break
				}
				preemptTask(victim, log)
			}
		}
		for countRunning(states) < req.Slots {
			next := highestPriorityWaiting(states)
			if next == nil {
				break
			}
			next.running = true
			kind := "start"
			if next.secured > 0 || next.preemptions > 0 {
				kind = "resume"
			}
			log(next.spec.ID, kind, fmt.Sprintf("occupies slot, resuming from %.4g/%.4g secured work units", next.secured, next.spec.Work))
		}
	}

	for _, s := range states {
		res.Tasks = append(res.Tasks, TaskResult{
			ID:                   s.spec.ID,
			Priority:             s.spec.Priority,
			Arrival:              s.spec.Arrival,
			Completion:           round(s.completion),
			Turnaround:           round(s.completion - s.spec.Arrival),
			CheckpointsCommitted: s.committed,
			CheckpointsAborted:   s.aborted,
			Preemptions:          s.preemptions,
			WorkLost:             round(s.workLost),
		})
		if s.completion > res.Makespan {
			res.Makespan = s.completion
		}
	}
	sort.SliceStable(res.Tasks, func(i, j int) bool { return res.Tasks[i].ID < res.Tasks[j].ID })
	res.Makespan = round(res.Makespan)
	return res, nil
}

// phaseTimeLeft returns how long the running task needs until its current
// phase (work segment or checkpoint save) ends.
func (s *taskState) phaseTimeLeft() float64 {
	if s.phase == phaseSaving {
		return s.saveRemaining
	}
	remaining := s.spec.Work - s.secured - s.segDone
	if remaining < s.spec.CheckpointInterval-s.segDone {
		return remaining
	}
	return s.spec.CheckpointInterval - s.segDone
}

func preemptTask(s *taskState, log func(task, kind, detail string)) {
	s.running = false
	s.preemptions++
	lost := s.segDone
	if s.phase == phaseSaving {
		// The save was in flight: it never completed, so this checkpoint
		// does not exist and can never be resumed from.
		s.aborted++
		lost = s.segDone // segment work is uncommitted either way
		s.phase = phaseWorking
		s.saveRemaining = 0
		log(s.spec.ID, "checkpoint_aborted", "checkpoint save interrupted by preemption; partial checkpoint discarded and unusable")
	}
	s.workLost += lost
	s.segDone = 0
	log(s.spec.ID, "preempted", fmt.Sprintf("lost %.4g uncommitted work units, rolled back to %.4g secured units", lost, s.secured))
}

func countRunning(states []*taskState) int {
	n := 0
	for _, s := range states {
		if s.running {
			n++
		}
	}
	return n
}

func lowestPriorityRunning(states []*taskState) *taskState {
	var best *taskState
	for _, s := range states {
		if !s.running {
			continue
		}
		if best == nil || lessImportant(s, best) {
			best = s
		}
	}
	return best
}

func highestPriorityWaiting(states []*taskState) *taskState {
	var best *taskState
	for _, s := range states {
		if !s.arrived || s.done || s.running {
			continue
		}
		if best == nil || lessImportant(best, s) {
			best = s
		}
	}
	return best
}

// lessImportant reports whether a should be scheduled after b:
// lower priority, later arrival, larger id.
func lessImportant(a, b *taskState) bool {
	if a.spec.Priority != b.spec.Priority {
		return a.spec.Priority < b.spec.Priority
	}
	if a.spec.Arrival != b.spec.Arrival {
		return a.spec.Arrival > b.spec.Arrival
	}
	return a.spec.ID > b.spec.ID
}

func round(x float64) float64 {
	return math.Round(x*1e9) / 1e9
}
