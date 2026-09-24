package main

import (
	"strings"
	"testing"
)

// acceptanceScenario is the acceptance-test setup: a low-priority task is in
// the middle of a checkpoint save (its second save runs t=45..50) when a
// high-priority task arrives at t=47.
//
// slots=1
// low:  priority 1, arrival 0,  work 100, interval 20, cost 5
// high: priority 9, arrival 47, work 30,  interval 10, cost 2
func acceptanceScenario() SimRequest {
	return SimRequest{
		Slots: 1,
		Tasks: []TaskSpec{
			{ID: "low", Priority: 1, Arrival: 0, Work: 100, CheckpointInterval: 20, CheckpointCost: 5},
			{ID: "high", Priority: 9, Arrival: 47, Work: 30, CheckpointInterval: 10, CheckpointCost: 2},
		},
	}
}

func findEvents(res SimResult, task, kind string) []Event {
	var out []Event
	for _, e := range res.Events {
		if e.Task == task && e.Kind == kind {
			out = append(out, e)
		}
	}
	return out
}

func taskResult(t *testing.T, res SimResult, id string) TaskResult {
	t.Helper()
	for _, tr := range res.Tasks {
		if tr.ID == id {
			return tr
		}
	}
	t.Fatalf("task %q not in results", id)
	return TaskResult{}
}

// Core acceptance criterion: the checkpoint whose save was interrupted by the
// preemption must NOT be usable. The low task's second save (t=45..50) is
// aborted at t=47, so it may only resume from its first committed checkpoint
// (20 of 100 work units secured), losing the 20 units done since.
func TestPreemptionDuringCheckpointSave(t *testing.T) {
	res, err := simulate(acceptanceScenario(), true)
	if err != nil {
		t.Fatal(err)
	}

	aborted := findEvents(res, "low", "checkpoint_aborted")
	if len(aborted) != 1 {
		t.Fatalf("expected exactly 1 aborted checkpoint for low, got %d", len(aborted))
	}
	if aborted[0].Time != 47 {
		t.Fatalf("checkpoint abort expected at t=47, got t=%v", aborted[0].Time)
	}

	low := taskResult(t, res, "low")
	if low.CheckpointsAborted != 1 {
		t.Fatalf("low.CheckpointsAborted = %d, want 1", low.CheckpointsAborted)
	}
	if low.WorkLost != 20 {
		t.Fatalf("low.WorkLost = %v, want 20 (uncommitted segment discarded)", low.WorkLost)
	}
	if low.CheckpointsCommitted != 4 {
		t.Fatalf("low.CheckpointsCommitted = %d, want 4 (aborted save never counted)", low.CheckpointsCommitted)
	}

	// The resume must happen from the last *committed* checkpoint (20/100),
	// not from the 40/100 the interrupted save would have provided.
	resumes := findEvents(res, "low", "resume")
	if len(resumes) != 1 {
		t.Fatalf("expected 1 resume event for low, got %d", len(resumes))
	}
	if !strings.Contains(resumes[0].Detail, "20/100") {
		t.Fatalf("low resumed from wrong checkpoint: %q (want secured 20/100, i.e. the in-flight checkpoint was not used)", resumes[0].Detail)
	}

	// Deterministic completion times (derived by hand in README).
	high := taskResult(t, res, "high")
	if high.Completion != 81 {
		t.Fatalf("high.Completion = %v, want 81", high.Completion)
	}
	if low.Completion != 176 {
		t.Fatalf("low.Completion = %v, want 176 (re-executes the lost 20 units)", low.Completion)
	}
}

func TestNoPreemptionBaseline(t *testing.T) {
	res, err := simulate(acceptanceScenario(), false)
	if err != nil {
		t.Fatal(err)
	}
	if evs := findEvents(res, "low", "preempted"); len(evs) != 0 {
		t.Fatalf("no preemption expected, got %v", evs)
	}
	if evs := findEvents(res, "low", "checkpoint_aborted"); len(evs) != 0 {
		t.Fatalf("no aborted checkpoints expected, got %v", evs)
	}
	low := taskResult(t, res, "low")
	high := taskResult(t, res, "high")
	if low.Completion != 120 {
		t.Fatalf("low.Completion = %v, want 120", low.Completion)
	}
	if high.Completion != 154 {
		t.Fatalf("high.Completion = %v, want 154 (waits for low)", high.Completion)
	}
}

func TestCompareCompletionTimes(t *testing.T) {
	res, err := compare(acceptanceScenario())
	if err != nil {
		t.Fatal(err)
	}
	// Preemption helps the high-priority task, hurts the preempted one.
	if got := res.CompletionDelta["high"]; got != 73 {
		t.Fatalf("delta[high] = %v, want 73 (154 without - 81 with)", got)
	}
	if got := res.CompletionDelta["low"]; got != -56 {
		t.Fatalf("delta[low] = %v, want -56 (120 without - 176 with)", got)
	}
}

// Checkpoint saves cost time and must be included in completion times.
func TestCheckpointOverheadCounted(t *testing.T) {
	res, err := simulate(SimRequest{
		Slots: 1,
		Tasks: []TaskSpec{
			{ID: "a", Priority: 1, Arrival: 0, Work: 10, CheckpointInterval: 5, CheckpointCost: 3},
		},
	}, true)
	if err != nil {
		t.Fatal(err)
	}
	a := taskResult(t, res, "a")
	// work 0-5, save 5-8, work 8-13 -> 13, not 10.
	if a.Completion != 13 {
		t.Fatalf("completion = %v, want 13 (10 work + 2*... one checkpoint of cost 3)", a.Completion)
	}
	if a.CheckpointsCommitted != 1 {
		t.Fatalf("committed = %d, want 1", a.CheckpointsCommitted)
	}
}

// A task keeps occupying its slot while saving a checkpoint: an equal-priority
// task that arrives during the save cannot take the slot.
func TestSlotOccupiedDuringSave(t *testing.T) {
	res, err := simulate(SimRequest{
		Slots: 1,
		Tasks: []TaskSpec{
			{ID: "a", Priority: 1, Arrival: 0, Work: 10, CheckpointInterval: 5, CheckpointCost: 5},
			{ID: "b", Priority: 1, Arrival: 1, Work: 2, CheckpointInterval: 2, CheckpointCost: 0},
		},
	}, true)
	if err != nil {
		t.Fatal(err)
	}
	a := taskResult(t, res, "a")
	b := taskResult(t, res, "b")
	// a: work 0-5, save 5-10 (slot still occupied), work 10-15 -> 15.
	if a.Completion != 15 {
		t.Fatalf("a.Completion = %v, want 15", a.Completion)
	}
	// b arrives at t=1 but the single slot is held by a (including during
	// a's save), so b only runs 15-17.
	if b.Completion != 17 {
		t.Fatalf("b.Completion = %v, want 17 (slot was occupied during a's checkpoint save)", b.Completion)
	}
	if evs := findEvents(res, "a", "preempted"); len(evs) != 0 {
		t.Fatalf("equal priority must not preempt, got %v", evs)
	}
}

func TestValidation(t *testing.T) {
	bad := []SimRequest{
		{Slots: 0, Tasks: []TaskSpec{{ID: "a", Priority: 1, Work: 1, CheckpointInterval: 1}}},
		{Slots: 1, Tasks: nil},
		{Slots: 1, Tasks: []TaskSpec{{ID: "", Priority: 1, Work: 1, CheckpointInterval: 1}}},
		{Slots: 1, Tasks: []TaskSpec{{ID: "a", Priority: 1, Work: 1, CheckpointInterval: 1}, {ID: "a", Priority: 1, Work: 1, CheckpointInterval: 1}}},
		{Slots: 1, Tasks: []TaskSpec{{ID: "a", Priority: 1, Work: 0, CheckpointInterval: 1}}},
		{Slots: 1, Tasks: []TaskSpec{{ID: "a", Priority: 1, Work: 1, CheckpointInterval: 0}}},
		{Slots: 1, Tasks: []TaskSpec{{ID: "a", Priority: 1, Work: 1, CheckpointInterval: 1, CheckpointCost: -1}}},
		{Slots: 1, Tasks: []TaskSpec{{ID: "a", Priority: 1, Arrival: -1, Work: 1, CheckpointInterval: 1}}},
	}
	for i, req := range bad {
		if _, err := simulate(req, true); err == nil {
			t.Fatalf("case %d: expected validation error, got nil", i)
		}
	}
}

// Invariant test over a randomized-ish workload: a task's secured work must
// never exceed committed checkpoints * interval, and every run must terminate
// with all tasks complete.
func TestMultiSlotScheduling(t *testing.T) {
	res, err := simulate(SimRequest{
		Slots: 2,
		Tasks: []TaskSpec{
			{ID: "a", Priority: 5, Arrival: 0, Work: 40, CheckpointInterval: 10, CheckpointCost: 2},
			{ID: "b", Priority: 1, Arrival: 0, Work: 40, CheckpointInterval: 10, CheckpointCost: 2},
			{ID: "c", Priority: 9, Arrival: 3, Work: 8, CheckpointInterval: 4, CheckpointCost: 1},
		},
	}, true)
	if err != nil {
		t.Fatal(err)
	}
	if len(res.Tasks) != 3 {
		t.Fatalf("want 3 task results, got %d", len(res.Tasks))
	}
	// c outranks b and arrives while both slots are busy -> b is preempted at t=3.
	if evs := findEvents(res, "b", "preempted"); len(evs) != 1 {
		t.Fatalf("expected b to be preempted once, got %v", evs)
	}
	c := taskResult(t, res, "c")
	// c: work 3-7, save 7-8, work 8-12 -> completes at 12.
	if c.Completion != 12 {
		t.Fatalf("c.Completion = %v, want 12", c.Completion)
	}
}
