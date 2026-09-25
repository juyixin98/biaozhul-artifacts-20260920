package deadlineadm

import (
	"testing"
	"time"
)

// t0 is the simulation origin used by the hand-computed task sets.
var t0 = time.Unix(0, 0).UTC()

func ms(n int64) time.Time      { return t0.Add(time.Duration(n) * time.Millisecond) }
func dur(n int64) time.Duration { return time.Duration(n) * time.Millisecond }

func item(tag string, demand int, deadline int64, budget int64) WorkItem {
	return WorkItem{Tag: tag, Demand: demand, Release: t0, Deadline: ms(deadline), Remaining: dur(budget)}
}

// TestSimulate_HandComputedEDF drives the admission simulation with task sets
// whose EDF schedules were computed by hand (single unit-capacity machine,
// all release at t=0).
func TestSimulate_HandComputedEDF(t *testing.T) {
	t.Run("three feasible jobs complete at 30/80/100", func(t *testing.T) {
		// j1 b=30 d=100, j2 b=50 d=80, j3 b=20 d=120
		// EDF: j1(100) -> j2(80): order by deadline is j2(d80) first!
		// order: j2 [0,50], j1 [50,80], j3 [80,100]
		items := []WorkItem{
			item("j1", 1, 100, 30),
			item("j2", 1, 80, 50),
			item("j3", 1, 120, 20),
		}
		res := SimulateEDF(t0, 1, items)
		if !res.Feasible {
			t.Fatalf("expected feasible, got rejection on %q: %s", res.MissedOn, res.Reason)
		}
		want := map[string]int64{"j2": 50, "j1": 80, "j3": 100}
		if len(res.Completions) != 3 {
			t.Fatalf("want 3 completions, got %d", len(res.Completions))
		}
		for _, c := range res.Completions {
			if c.Completion.UnixMilli() != want[c.Tag] {
				t.Errorf("job %s completes at %d, want %d", c.Tag, c.Completion.UnixMilli(), want[c.Tag])
			}
			if c.Missed {
				t.Errorf("job %s marked missed", c.Tag)
			}
		}
	})

	t.Run("overload rejected as predicted infeasible", func(t *testing.T) {
		// Total demand 130 within first deadline 100:
		// j1 b=60 d=100, j2 b=40 d=100, j3 b=30 d=130
		// EDF: j1,j2 [0..100] exactly, j3 [100,130] exactly -> feasible.
		// Tighten: j3 d=120 b=30 => j3 finishes 130 > 120 => infeasible.
		items := []WorkItem{
			item("j1", 1, 100, 60),
			item("j2", 1, 100, 40),
			item("j3", 1, 120, 30),
		}
		res := SimulateEDF(t0, 1, items)
		if res.Feasible {
			t.Fatalf("expected infeasible, completions=%v", res.Completions)
		}
		if res.MissedOn != "j3" {
			t.Fatalf("want infeasibility blamed on j3, got %q (%s)", res.MissedOn, res.Reason)
		}
	})

	t.Run("tight fit exactly at deadlines is feasible", func(t *testing.T) {
		items := []WorkItem{
			item("a", 1, 50, 50),
			item("b", 1, 100, 50),
		}
		res := SimulateEDF(t0, 1, items)
		if !res.Feasible {
			t.Fatalf("exact-deadline fit must be feasible: %s", res.Reason)
		}
	})

	t.Run("single job budget exceeding horizon rejected before simulation", func(t *testing.T) {
		items := []WorkItem{item("x", 1, 50, 60)}
		res := SimulateEDF(t0, 1, items)
		if res.Feasible || res.MissedOn != "x" {
			t.Fatalf("want x rejected, got %+v", res)
		}
	})
}

// TestSimulate_ParallelCapacity verifies the multi-unit machine: jobs with
// total demand within capacity run in parallel.
func TestSimulate_ParallelCapacity(t *testing.T) {
	t.Run("two demand-1 jobs on capacity 2 finish together", func(t *testing.T) {
		items := []WorkItem{
			item("a", 1, 50, 40),
			item("b", 1, 60, 40),
		}
		res := SimulateEDF(t0, 2, items)
		if !res.Feasible {
			t.Fatalf("want parallel feasible: %s", res.Reason)
		}
		for _, c := range res.Completions {
			if c.Completion.UnixMilli() != 40 {
				t.Errorf("job %s at %d, want 40 (parallel)", c.Tag, c.Completion.UnixMilli())
			}
		}
	})

	t.Run("demand above capacity rejected structurally", func(t *testing.T) {
		items := []WorkItem{item("big", 3, 100, 10)}
		res := SimulateEDF(t0, 2, items)
		if res.Feasible {
			t.Fatal("demand>capacity must reject")
		}
	})

	t.Run("head blocks later jobs even if a later job fits", func(t *testing.T) {
		// capacity 2, all release 0:
		//   z demand1 budget10 deadline60
		//   x demand2 budget40 deadline100 (inserted before y)
		//   y demand1 budget10 deadline100
		// EDF dispatch at t=0: z starts (in-use 1). Next is x, which would
		// raise in-use to 3 > 2: x is head and BLOCKS — y cannot skip it even
		// though y would fit in the spare unit. z runs [0,10]; then x gets
		// both units [10,50]; y last [50,60]. This conservative head-blocks
		// rule is shared by the admission sim and the live pump.
		items := []WorkItem{
			item("x", 2, 100, 40),
			item("y", 1, 100, 10),
			item("z", 1, 60, 10),
		}
		res := SimulateEDF(t0, 2, items)
		if !res.Feasible {
			t.Fatalf("want feasible: %s", res.Reason)
		}
		want := map[string]int64{"z": 10, "x": 50, "y": 60}
		for _, c := range res.Completions {
			if c.Completion.UnixMilli() != want[c.Tag] {
				t.Errorf("%s at %d want %d", c.Tag, c.Completion.UnixMilli(), want[c.Tag])
			}
		}
	})
}

// TestSimulate_FutureRelease checks release-time handling.
func TestSimulate_FutureRelease(t *testing.T) {
	// a release 0 b40 d50; b release 30 b40 d80.
	// a runs [0,40], b released 30 waits, runs [40,80] exact -> feasible.
	items := []WorkItem{
		{Tag: "a", Demand: 1, Release: t0, Deadline: ms(50), Remaining: dur(40)},
		{Tag: "b", Demand: 1, Release: ms(30), Deadline: ms(80), Remaining: dur(40)},
	}
	res := SimulateEDF(t0, 1, items)
	if !res.Feasible {
		t.Fatalf("want feasible: %s", res.Reason)
	}
	want := map[string]int64{"a": 40, "b": 80}
	for _, c := range res.Completions {
		if c.Completion.UnixMilli() != want[c.Tag] {
			t.Errorf("%s at %d want %d", c.Tag, c.Completion.UnixMilli(), want[c.Tag])
		}
	}

	// b released too late to finish.
	items[1].Release = ms(50)
	items[1].Deadline = ms(80) // 40 work, 30 available -> miss
	res = SimulateEDF(t0, 1, items)
	if res.Feasible {
		t.Fatal("late-released job must be infeasible")
	}
}

// TestSimulate_RunningResidual verifies the scheduler's residual construction
// for already-running jobs under both overrun policies.
func TestSimulate_RunningResidual(t *testing.T) {
	now := ms(30)
	// running job r started 0, budget 60 -> residual 30, deadline 90.
	// candidate c budget 50, deadline 90: on capacity 1 after r finishes at
	// 60, c runs [60,110] -> miss 90 => reject.
	items := []WorkItem{
		{Tag: "r", Demand: 1, Release: now, Deadline: ms(90), Remaining: dur(30)},
		{Tag: "c", Demand: 1, Release: now, Deadline: ms(90), Remaining: dur(50)},
	}
	res := SimulateEDF(now, 1, items)
	if res.Feasible {
		t.Fatal("candidate must be rejected behind running residual")
	}
	// Give c enough room: deadline 120 -> r [30,60], c [60,110] feasible.
	items[1].Deadline = ms(120)
	res = SimulateEDF(now, 1, items)
	if !res.Feasible {
		t.Fatalf("want feasible: %s", res.Reason)
	}
}
