package sim

import (
	"strings"
	"testing"
)

// acceptanceScenario is the scenario from the acceptance criteria: the
// high-priority task H arrives while the low-priority task L is in the middle
// of a checkpoint save.
//
//	capacity = 2
//	L: prio 1,  arrives t=0, work 12, checkpoint every 4, save cost 3, demand 2
//	H: prio 10, arrives t=6, work 8,  checkpoint every 4, save cost 1, demand 2
//
// L's first save spans ticks 4..6 and would commit at t=7. H arrives at t=6,
// so the save is aborted one tick before it would have committed.
func acceptanceScenario() (int, []TaskSpec) {
	return 2, []TaskSpec{
		{ID: "L", Priority: 1, ArrivalTick: 0, TotalWork: 12, CheckpointEvery: 4, CheckpointCost: 3, ResourceDemand: 2},
		{ID: "H", Priority: 10, ArrivalTick: 6, TotalWork: 8, CheckpointEvery: 4, CheckpointCost: 1, ResourceDemand: 2},
	}
}

func findEvent(tl []Event, typ, id string) *Event {
	for i := range tl {
		if tl[i].Type == typ && tl[i].TaskID == id {
			return &tl[i]
		}
	}
	return nil
}

func taskResult(t *testing.T, r RunResult, id string) TaskResult {
	t.Helper()
	for _, tr := range r.Tasks {
		if tr.ID == id {
			return tr
		}
	}
	t.Fatalf("task %q missing from results", id)
	return TaskResult{}
}

// TestAcceptancePreemptDuringSave is the acceptance test: a high-priority task
// arriving mid-save must preempt, must NOT benefit from the victim's
// not-yet-committed checkpoint, and the response must carry the
// preemptive/non-preemptive completion comparison.
func TestAcceptancePreemptDuringSave(t *testing.T) {
	capacity, tasks := acceptanceScenario()
	res, err := Simulate(capacity, tasks)
	if err != nil {
		t.Fatalf("Simulate: %v", err)
	}
	pre := res.Preemptive

	// 1. L was preempted at t=6 while its checkpoint save was in flight.
	ev := findEvent(pre.Timeline, EvPreempt, "L")
	if ev == nil {
		t.Fatalf("expected a preempt event for L, timeline: %+v", pre.Timeline)
	}
	if ev.Tick != 6 {
		t.Errorf("preempt tick = %d, want 6", ev.Tick)
	}
	if !strings.Contains(ev.Detail, "in_flight_checkpoint_discarded") {
		t.Errorf("preempt detail = %q, want it to mention the discarded in-flight checkpoint", ev.Detail)
	}

	// 2. The interrupted save never committed: no checkpoint_committed event
	//    for L at or before the preemption tick.
	for _, e := range pre.Timeline {
		if e.Type == EvCheckpoint && e.TaskID == "L" && e.Tick <= 6 {
			t.Errorf("L committed a checkpoint at tick %d before/during preemption — in-flight save must not commit", e.Tick)
		}
	}

	// 3. L resumes from its last COMMITTED checkpoint, which is work=0 —
	//    provably not the in-flight save's target (work=4).
	rv := findEvent(pre.Timeline, EvResume, "L")
	if rv == nil {
		t.Fatalf("expected a resume event for L")
	}
	if rv.FromCheckpoint != 0 {
		t.Errorf("L resumed from checkpoint %d, want 0 (the in-flight checkpoint at 4 must not be used)", rv.FromCheckpoint)
	}

	// 4. Accounting: 2 save ticks wasted (t=4,5), 4 work units redone.
	lt := taskResult(t, pre, "L")
	if lt.WastedSaveTicks != 2 {
		t.Errorf("L wasted_save_ticks = %d, want 2", lt.WastedSaveTicks)
	}
	if lt.RedundantWork != 4 {
		t.Errorf("L redundant_work = %d, want 4", lt.RedundantWork)
	}
	if lt.PreemptedCount != 1 {
		t.Errorf("L preempted_count = %d, want 1", lt.PreemptedCount)
	}

	// 5. Completion-time comparison between policies.
	hp := taskResult(t, pre, "H")
	hn := taskResult(t, res.NonPreemptive, "H")
	if hp.CompletionTick != 15 {
		t.Errorf("preemptive H completion = %d, want 15", hp.CompletionTick)
	}
	if hn.CompletionTick != 27 {
		t.Errorf("non-preemptive H completion = %d, want 27", hn.CompletionTick)
	}
	if hp.CompletionTick >= hn.CompletionTick {
		t.Errorf("preemption did not help H: preemptive=%d non-preemptive=%d", hp.CompletionTick, hn.CompletionTick)
	}

	// 6. The comparison block reports the per-task delta.
	var hcmp *TaskComparison
	for i := range res.TaskComparison {
		if res.TaskComparison[i].ID == "H" {
			hcmp = &res.TaskComparison[i]
		}
	}
	if hcmp == nil {
		t.Fatalf("task_comparison missing H")
	}
	if hcmp.DeltaTicks != 12 {
		t.Errorf("H delta_ticks = %d, want 12 (27-15)", hcmp.DeltaTicks)
	}

	// 7. Makespans: preemption costs overall time here (lost work + repeated
	//    saves) — recorded honestly rather than assumed to be a win.
	if pre.Makespan != 33 {
		t.Errorf("preemptive makespan = %d, want 33", pre.Makespan)
	}
	if res.NonPreemptive.Makespan != 27 {
		t.Errorf("non-preemptive makespan = %d, want 27", res.NonPreemptive.Makespan)
	}
	if res.MakespanDelta != 27-33 {
		t.Errorf("makespan_delta = %d, want %d", res.MakespanDelta, 27-33)
	}
}

// TestZeroCostCheckpointCommitsImmediately covers checkpoint_cost=0: the save
// commits in the same tick and is a valid resume point.
func TestZeroCostCheckpointCommitsImmediately(t *testing.T) {
	res, err := Simulate(1, []TaskSpec{
		{ID: "A", Priority: 1, ArrivalTick: 0, TotalWork: 2, CheckpointEvery: 1, CheckpointCost: 0, ResourceDemand: 1},
	})
	if err != nil {
		t.Fatalf("Simulate: %v", err)
	}
	pre := res.Preemptive
	ev := findEvent(pre.Timeline, EvCheckpoint, "A")
	if ev == nil {
		t.Fatalf("expected a committed checkpoint, timeline: %+v", pre.Timeline)
	}
	if ev.Tick != 0 || ev.FromCheckpoint != 1 {
		t.Errorf("checkpoint event = %+v, want tick 0 from_checkpoint 1", *ev)
	}
	if pre.Makespan != 2 {
		t.Errorf("makespan = %d, want 2", pre.Makespan)
	}
}

// TestCapacityNeverExceeded reconstructs per-tick occupancy from the event
// stream and asserts the cluster capacity is never violated, in both policies.
func TestCapacityNeverExceeded(t *testing.T) {
	capacity := 3
	tasks := []TaskSpec{
		{ID: "a", Priority: 1, ArrivalTick: 0, TotalWork: 9, CheckpointEvery: 3, CheckpointCost: 2, ResourceDemand: 2},
		{ID: "b", Priority: 5, ArrivalTick: 1, TotalWork: 6, CheckpointEvery: 2, CheckpointCost: 1, ResourceDemand: 2},
		{ID: "c", Priority: 9, ArrivalTick: 2, TotalWork: 4, CheckpointEvery: 2, CheckpointCost: 1, ResourceDemand: 1},
		{ID: "d", Priority: 3, ArrivalTick: 3, TotalWork: 5, CheckpointEvery: 5, CheckpointCost: 0, ResourceDemand: 1},
	}
	res, err := Simulate(capacity, tasks)
	if err != nil {
		t.Fatalf("Simulate: %v", err)
	}
	for _, run := range []RunResult{res.Preemptive, res.NonPreemptive} {
		if !run.AllCompleted {
			t.Fatalf("%s: not all tasks completed", run.Policy)
		}
		// Build [start,end) occupancy intervals per task from events.
		type iv struct{ start, end, demand int64 }
		var ivs []iv
		open := map[string]int64{}
		demand := map[string]int64{}
		for _, ts := range tasks {
			demand[ts.ID] = int64(ts.ResourceDemand)
		}
		for _, e := range run.Timeline {
			switch e.Type {
			case EvStart, EvResume:
				open[e.TaskID] = e.Tick
			case EvPreempt, EvComplete:
				s, ok := open[e.TaskID]
				if !ok {
					t.Fatalf("%s: %s for %s without open interval", run.Policy, e.Type, e.TaskID)
				}
				ivs = append(ivs, iv{s, e.Tick, demand[e.TaskID]})
				delete(open, e.TaskID)
			}
		}
		for tick := int64(0); tick <= run.Makespan; tick++ {
			var used int64
			for _, v := range ivs {
				if v.start <= tick && tick < v.end {
					used += v.demand
				}
			}
			if used > int64(capacity) {
				t.Errorf("%s: capacity exceeded at tick %d: used %d > %d", run.Policy, tick, used, capacity)
			}
		}
	}
}

// TestNonPreemptiveIgnoresPriority verifies the non-preemptive policy never
// evicts, while the preemptive one does, on the same input.
func TestNonPreemptiveIgnoresPriority(t *testing.T) {
	res, err := Simulate(1, []TaskSpec{
		{ID: "low", Priority: 1, ArrivalTick: 0, TotalWork: 3, CheckpointEvery: 10, CheckpointCost: 0, ResourceDemand: 1},
		{ID: "high", Priority: 9, ArrivalTick: 1, TotalWork: 1, CheckpointEvery: 10, CheckpointCost: 0, ResourceDemand: 1},
	})
	if err != nil {
		t.Fatalf("Simulate: %v", err)
	}
	// Non-preemptive: low finishes at 3, high waits and finishes at 4.
	if got := taskResult(t, res.NonPreemptive, "low").CompletionTick; got != 3 {
		t.Errorf("non-preemptive low completion = %d, want 3", got)
	}
	if got := taskResult(t, res.NonPreemptive, "high").CompletionTick; got != 4 {
		t.Errorf("non-preemptive high completion = %d, want 4", got)
	}
	if res.NonPreemptive.Totals.Preemptions != 0 {
		t.Errorf("non-preemptive preemptions = %d, want 0", res.NonPreemptive.Totals.Preemptions)
	}
	// Preemptive: high evicts low at t=1, finishes at 2; low redoes 1 unit,
	// resumes at t=2 from checkpoint 0 and finishes at 5.
	if got := taskResult(t, res.Preemptive, "high").CompletionTick; got != 2 {
		t.Errorf("preemptive high completion = %d, want 2", got)
	}
	low := taskResult(t, res.Preemptive, "low")
	if low.CompletionTick != 5 {
		t.Errorf("preemptive low completion = %d, want 5", low.CompletionTick)
	}
	if low.RedundantWork != 1 {
		t.Errorf("low redundant_work = %d, want 1", low.RedundantWork)
	}
}

// TestEqualPriorityNeverPreempts: ties in priority must not evict each other.
func TestEqualPriorityNeverPreempts(t *testing.T) {
	res, err := Simulate(1, []TaskSpec{
		{ID: "a", Priority: 5, ArrivalTick: 0, TotalWork: 2, CheckpointEvery: 10, CheckpointCost: 0, ResourceDemand: 1},
		{ID: "b", Priority: 5, ArrivalTick: 1, TotalWork: 1, CheckpointEvery: 10, CheckpointCost: 0, ResourceDemand: 1},
	})
	if err != nil {
		t.Fatalf("Simulate: %v", err)
	}
	if res.Preemptive.Totals.Preemptions != 0 {
		t.Errorf("equal-priority preemptions = %d, want 0", res.Preemptive.Totals.Preemptions)
	}
	if got := taskResult(t, res.Preemptive, "b").CompletionTick; got != 3 {
		t.Errorf("b completion = %d, want 3 (waits for a)", got)
	}
}

// TestValidation exercises input validation.
func TestValidation(t *testing.T) {
	good := TaskSpec{ID: "x", Priority: 1, ArrivalTick: 0, TotalWork: 1, CheckpointEvery: 1, CheckpointCost: 0, ResourceDemand: 1}
	cases := []struct {
		name     string
		capacity int
		tasks    []TaskSpec
	}{
		{"zero capacity", 0, []TaskSpec{good}},
		{"empty id", 1, []TaskSpec{{ID: "", TotalWork: 1, CheckpointEvery: 1, ResourceDemand: 1}}},
		{"duplicate id", 1, []TaskSpec{good, good}},
		{"negative arrival", 1, []TaskSpec{func() TaskSpec { s := good; s.ArrivalTick = -1; return s }()}},
		{"zero work", 1, []TaskSpec{func() TaskSpec { s := good; s.TotalWork = 0; return s }()}},
		{"zero interval", 1, []TaskSpec{func() TaskSpec { s := good; s.CheckpointEvery = 0; return s }()}},
		{"negative cost", 1, []TaskSpec{func() TaskSpec { s := good; s.CheckpointCost = -1; return s }()}},
		{"zero demand", 1, []TaskSpec{func() TaskSpec { s := good; s.ResourceDemand = 0; return s }()}},
		{"demand exceeds capacity", 1, []TaskSpec{func() TaskSpec { s := good; s.ResourceDemand = 2; return s }()}},
	}
	for _, c := range cases {
		if _, err := Simulate(c.capacity, c.tasks); err == nil {
			t.Errorf("%s: expected validation error, got nil", c.name)
		}
	}
	if _, err := Simulate(1, []TaskSpec{good}); err != nil {
		t.Errorf("valid input rejected: %v", err)
	}
}

// TestDeterminism: identical inputs must produce identical outputs.
func TestDeterminism(t *testing.T) {
	capacity, tasks := acceptanceScenario()
	a, err := Simulate(capacity, tasks)
	if err != nil {
		t.Fatalf("Simulate: %v", err)
	}
	b, err := Simulate(capacity, tasks)
	if err != nil {
		t.Fatalf("Simulate: %v", err)
	}
	if len(a.Preemptive.Timeline) != len(b.Preemptive.Timeline) {
		t.Fatalf("timeline length differs between runs")
	}
	for i := range a.Preemptive.Timeline {
		if a.Preemptive.Timeline[i] != b.Preemptive.Timeline[i] {
			t.Fatalf("timeline diverges at %d: %+v vs %+v", i, a.Preemptive.Timeline[i], b.Preemptive.Timeline[i])
		}
	}
}
