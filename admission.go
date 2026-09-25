package deadlineadm

import (
	"fmt"
	"sort"
	"time"
)

// WorkItem is one unit of unfinished execution fed to the admission
// simulation. It deliberately mirrors what the real scheduler knows:
//
//   - for the candidate and for queued jobs, Remaining is the declared budget;
//   - for a running job in kill_at_budget mode, Remaining is the unspent part
//     of its declared budget;
//   - for a running job in observe mode, Remaining is conservatively capped at
//     the time until its deadline (the safety bound the scheduler enforces).
type WorkItem struct {
	Tag       string
	Demand    int
	Release   time.Time
	Deadline  time.Time
	Remaining time.Duration
}

// Completion records a simulated finish time.
type Completion struct {
	Tag        string
	Completion time.Time
	Missed     bool
}

// AdmissionResult is the outcome of the EDF feasibility simulation.
type AdmissionResult struct {
	Feasible bool
	// MissedOn names the first item found unable to meet its deadline.
	MissedOn string
	// Reason gives the human-readable infeasibility explanation.
	Reason string
	// Completions lists simulated finish times in chronological order.
	Completions []Completion
}

type simItem struct {
	WorkItem
	id        int
	remaining time.Duration
	started   bool
	done      bool
	completed time.Time
}

// SimulateEDF runs an event-driven, non-preemptive EDF schedule starting at
// now on a machine of the given capacity and reports whether every item
// completes by its deadline.
//
// The simulation is deterministic and uses exactly the dispatch rule the real
// scheduler follows: among released, unfinished jobs, earliest deadline first
// (FIFO on ties); a job at the head that does not fit blocks later jobs; jobs
// run in parallel while total demand fits. An item whose deadline arrives
// while it is unfinished is infeasible — whether it is still queued or already
// running non-preemptibly.
func SimulateEDF(now time.Time, capacity int, items []WorkItem) AdmissionResult {
	if capacity <= 0 {
		return AdmissionResult{Feasible: false, Reason: "capacity must be positive"}
	}

	sim := make([]*simItem, len(items))
	for i, it := range items {
		if it.Demand > capacity {
			return AdmissionResult{
				Feasible: false, MissedOn: it.Tag,
				Reason: fmt.Sprintf("job %q demands %d units but machine capacity is %d", it.Tag, it.Demand, capacity),
			}
		}
		if it.Demand <= 0 || it.Remaining <= 0 {
			return AdmissionResult{
				Feasible: false, MissedOn: it.Tag,
				Reason: fmt.Sprintf("job %q has non-positive demand or execution bound", it.Tag),
			}
		}
		sim[i] = &simItem{WorkItem: it, id: i, remaining: it.Remaining}
	}

	// pending: release strictly in the future; waiting: released, not started;
	// running: dispatched and consuming capacity.
	var pending, waiting, running []*simItem
	for _, s := range sim {
		if s.Release.After(now) {
			pending = append(pending, s)
		} else {
			waiting = append(waiting, s)
		}
	}
	sort.Slice(pending, func(i, j int) bool {
		if !pending[i].Release.Equal(pending[j].Release) {
			return pending[i].Release.Before(pending[j].Release)
		}
		return pending[i].id < pending[j].id
	})

	t := now
	inUse := 0
	finishedCount := 0

	complete := func(s *simItem) {
		s.done = true
		s.completed = t
		finishedCount++
	}

	for finishedCount < len(sim) {
		// 1) Absorb any releases due at t.
		absorbed := 0
		for _, s := range pending {
			if s.Release.After(t) {
				break
			}
			waiting = append(waiting, s)
			absorbed++
		}
		pending = pending[absorbed:]

		// 2) Deadline check: every unfinished item must still have time.
		for _, s := range sim {
			if !s.done && !s.Deadline.After(t) {
				return infeasible(s, sim, t)
			}
		}

		// 3) Dispatch EDF jobs that fit, head-blocks-queue.
		sort.SliceStable(waiting, func(i, j int) bool {
			if !waiting[i].Deadline.Equal(waiting[j].Deadline) {
				return waiting[i].Deadline.Before(waiting[j].Deadline)
			}
			return waiting[i].id < waiting[j].id
		})
		dispatched := 0
		for dispatched < len(waiting) {
			s := waiting[dispatched]
			if inUse+s.Demand > capacity {
				break // head blocks every later job, regardless of fit
			}
			inUse += s.Demand
			s.started = true
			running = append(running, s)
			dispatched++
		}
		waiting = waiting[dispatched:]

		// 4) Choose the next event time.
		var nextT time.Time
		have := false
		note := func(x time.Time) {
			if !have || x.Before(nextT) {
				nextT, have = x, true
			}
		}
		for _, s := range running {
			note(t.Add(s.remaining))
		}
		if len(pending) > 0 {
			note(pending[0].Release)
		}
		for _, s := range sim {
			if !s.done {
				note(s.Deadline)
			}
		}
		if !have {
			return AdmissionResult{
				Feasible: false,
				Reason:   "simulation stalled with unfinished jobs",
			}
		}

		dt := nextT.Sub(t)
		t = nextT

		// 5) Progress running work; handle completions at t.
		still := running[:0]
		for _, s := range running {
			s.remaining -= dt
			if s.remaining <= 0 {
				inUse -= s.Demand
				complete(s)
			} else {
				still = append(still, s)
			}
		}
		running = still
	}

	out := make([]Completion, 0, len(sim))
	for _, s := range sim {
		out = append(out, Completion{Tag: s.Tag, Completion: s.completed, Missed: s.completed.After(s.Deadline)})
	}
	sort.Slice(out, func(i, j int) bool {
		if !out[i].Completion.Equal(out[j].Completion) {
			return out[i].Completion.Before(out[j].Completion)
		}
		return out[i].Tag < out[j].Tag
	})
	return AdmissionResult{Feasible: true, Completions: out}
}

func infeasible(s *simItem, all []*simItem, t time.Time) AdmissionResult {
	var out []Completion
	for _, x := range all {
		if x.done {
			out = append(out, Completion{Tag: x.Tag, Completion: x.completed, Missed: x.completed.After(x.Deadline)})
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Completion.Before(out[j].Completion) })
	return AdmissionResult{
		Feasible:    false,
		MissedOn:    s.Tag,
		Reason:      fmt.Sprintf("job %q cannot meet deadline %d ms (time already %d ms)", s.Tag, s.Deadline.UnixMilli(), t.UnixMilli()),
		Completions: out,
	}
}
