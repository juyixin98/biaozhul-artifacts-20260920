package scheduler_test

import (
	"reflect"
	"testing"

	"pim/internal/event"
	"pim/internal/program"
	"pim/internal/scenario"
	"pim/internal/scheduler"
)

// runSpec is a short helper.
func runSpec(t *testing.T, sp scheduler.Spec) *scheduler.Result {
	t.Helper()
	res, err := scheduler.Run(sp, nil, nil)
	if err != nil {
		t.Fatalf("Run returned error: %v", err)
	}
	return res
}

// findPriority returns the effective priority of task id in the final
// snapshot.
func findPriority(t *testing.T, res *scheduler.Result, id string) int {
	t.Helper()
	for _, ts := range res.Tasks {
		if ts.ID == id {
			return ts.EffPriority
		}
	}
	t.Fatalf("task %s not found in result", id)
	return 0
}

// priorityTransitions collects (old,new) effective-priority pairs from
// priority_change events for one task, in order.
func priorityTransitions(res *scheduler.Result, id string) [][2]int {
	var out [][2]int
	for _, e := range res.Events {
		if e.Kind == event.PriorityChange && e.Task == id {
			out = append(out, [2]int{
				e.Detail["old"].(int),
				e.Detail["new"].(int),
			})
		}
	}
	return out
}

// TestClassicInversionWithoutPI proves the bug exists: with inheritance off,
// Medium runs while High is blocked on Low's lock, and High finishes later.
func TestClassicInversionWithoutPI(t *testing.T) {
	res := runSpec(t, scenario.ClassicInversion(false))

	if got := res.Summary.InversionTicks; got == 0 {
		t.Fatalf("expected unbounded priority inversion ticks, got 0")
	} else {
		t.Logf("inversion ticks without PI: %d", got)
	}
	// High is blocked for the whole Medium burst (at least Medium's 4 ticks
	// plus Low's remaining critical section).
	if res.Summary.BlockedTicks["High"] < 8 {
		t.Errorf("High blocked %d ticks, expected >= 8 (Medium delay)",
			res.Summary.BlockedTicks["High"])
	}
	// Medium must have been dispatched at least twice while High was
	// blocked: find a Medium dispatch with High in blocked state via the
	// explicit inversion counter instead of reconstructing state.
	if res.FinishTime < 13 {
		t.Errorf("makespan %d unexpectedly short", res.FinishTime)
	}
}

// TestClassicInversionFixedWithPI proves the protocol fixes it: Low inherits
// High's priority, Medium cannot be dispatched during the critical section,
// inversion counter is zero and High finishes earlier than without PI.
func TestClassicInversionFixedWithPI(t *testing.T) {
	pi := runSpec(t, scenario.ClassicInversion(true))
	noPI := runSpec(t, scenario.ClassicInversion(false))

	if pi.Summary.InversionTicks != 0 {
		t.Errorf("PI run reported %d inversion ticks, want 0",
			pi.Summary.InversionTicks)
	}
	highPI := pi.Summary.CompletedAt["High"]
	highNoPI := noPI.Summary.CompletedAt["High"]
	if !(highPI < highNoPI) {
		t.Errorf("PI did not help High: with PI done=%d, without PI done=%d",
			highPI, highNoPI)
	}
	// Low must have inherited priority 3 at some point.
	tr := priorityTransitions(pi, "Low")
	if len(tr) == 0 {
		t.Fatal("expected Low priority changes, got none")
	}
	maxNew := 0
	for _, p := range tr {
		if p[1] > maxNew {
			maxNew = p[1]
		}
	}
	if maxNew != 3 {
		t.Errorf("Low max inherited priority = %d, want 3 (High)", maxNew)
	}
	// Medium finishes before High only in the no-PI run; with PI it runs
	// after the critical section unwinds.
	if pi.Summary.CompletedAt["Medium"] <= highPI {
		t.Errorf("Medium finished at %d before/equal High %d under PI",
			pi.Summary.CompletedAt["Medium"], highPI)
	}
}

// TestThreeLevelTransitiveInheritance verifies inheritance propagates TWO
// hops up a nested blocking chain: Low (pri1) -> High (pri3) -> Urgent
// (pri4), so Low reaches effective priority 4.
func TestThreeLevelTransitiveInheritance(t *testing.T) {
	res := runSpec(t, scenario.ThreeLevelInheritance())

	// Low: 1 -> 3 (High blocks on R1) -> 4 (Urgent blocks on R2 via High)
	//      -> 1 (releases R1; chain collapses).
	lowTr := priorityTransitions(res, "Low")
	want := [][2]int{{1, 3}, {3, 4}, {4, 1}}
	if !equalPairs(lowTr, want) {
		t.Errorf("Low priority transitions = %v, want %v", lowTr, want)
	}
	// High inherits 4 from Urgent, then restores to 3.
	highTr := priorityTransitions(res, "High")
	wantHigh := [][2]int{{3, 4}, {4, 3}}
	if !equalPairs(highTr, wantHigh) {
		t.Errorf("High priority transitions = %v, want %v", highTr, wantHigh)
	}
	for _, ts := range res.Tasks {
		if ts.State != scheduler.StDone {
			t.Errorf("task %s final state = %s, want done", ts.ID, ts.State)
		}
	}
}

// TestEffectivePriorityRecomputedOnRelease checks the single-lock case:
// inheritance is dropped exactly when the critical section ends, not later.
func TestEffectivePriorityRecomputedOnRelease(t *testing.T) {
	res := runSpec(t, scenario.InheritanceThenRestore())

	// While High is blocked, Low runs at 4; after unlock Low is back to 1.
	lowAt4, lowRestored := false, false
	for _, e := range res.Events {
		if e.Kind != event.PriorityChange || e.Task != "Low" {
			continue
		}
		if e.Detail["new"].(int) == 4 {
			lowAt4 = true
		}
		if e.Detail["new"].(int) == 1 && e.Detail["reason"] == "grant" {
			lowRestored = true
		}
	}
	if !lowAt4 || !lowRestored {
		t.Errorf("inherit=%v restore=%v, want both true", lowAt4, lowRestored)
	}
	if got := findPriority(t, res, "Low"); got != 1 {
		t.Errorf("final Low effective priority = %d, want 1", got)
	}
}

// TestMultiLockReleaseOrders runs the two release orders and asserts that
// grant ordering and per-task blocked accounting differ as expected, while
// both runs complete cleanly.
func TestMultiLockReleaseOrders(t *testing.T) {
	r1First := runSpec(t, scenario.MultiLockReleaseOrder("R1-first"))
	r2First := runSpec(t, scenario.MultiLockReleaseOrder("R2-first"))

	grants := func(res *scheduler.Result) []string {
		var out []string
		for _, e := range res.Events {
			if e.Kind == event.LockGrant {
				out = append(out, e.Resource)
			}
		}
		return out
	}
	if g := grants(r1First); len(g) < 2 || g[0] != "R1" {
		t.Errorf("R1-first grants = %v, want R1 first", g)
	}
	if g := grants(r2First); len(g) < 2 || g[0] != "R2" {
		t.Errorf("R2-first grants = %v, want R2 first", g)
	}
	// The waiter whose lock is released first blocks for fewer ticks.
	if r1First.Summary.BlockedTicks["A"] >= r2First.Summary.BlockedTicks["A"] {
		t.Errorf("A (waits R1) should block less in R1-first: %d vs %d",
			r1First.Summary.BlockedTicks["A"], r2First.Summary.BlockedTicks["A"])
	}
	if r2First.Summary.BlockedTicks["B"] >= r1First.Summary.BlockedTicks["B"] {
		t.Errorf("B (waits R2) should block less in R2-first: %d vs %d",
			r2First.Summary.BlockedTicks["B"], r1First.Summary.BlockedTicks["B"])
	}
	// Low inherited the max waiter priority (4) in both runs.
	for _, r := range []*scheduler.Result{r1First, r2First} {
		maxNew := 0
		for _, p := range priorityTransitions(r, "Low") {
			if p[1] > maxNew {
				maxNew = p[1]
			}
		}
		if maxNew != 4 {
			t.Errorf("Low max inherited priority = %d, want 4", maxNew)
		}
	}
}

// TestDeadlockDetected verifies the AB/BA cycle is caught and reported with a
// structured event, both tasks end blocked, and the cycle names the ring.
func TestDeadlockDetected(t *testing.T) {
	res := runSpec(t, scenario.DeadlockAB())

	if res.Deadlock == nil {
		t.Fatal("expected deadlock, got nil")
	}
	cyc := res.Deadlock.Cycle
	if len(cyc) < 3 || cyc[0] != cyc[len(cyc)-1] {
		t.Errorf("cycle %v does not form a closed ring", cyc)
	}
	var sawDeadlockEvent bool
	for _, e := range res.Events {
		if e.Kind == event.Deadlock {
			sawDeadlockEvent = true
			if e.Time != res.Deadlock.At {
				t.Errorf("deadlock event time %d != info.At %d", e.Time, res.Deadlock.At)
			}
		}
	}
	if !sawDeadlockEvent {
		t.Error("no deadlock event emitted")
	}
	for _, ts := range res.Tasks {
		if ts.State != scheduler.StBlocked {
			t.Errorf("task %s state = %s, want blocked", ts.ID, ts.State)
		}
	}
}

// TestDeadlockDetectionDisabledTerminatesWithError proves that with the
// detector off the simulation cannot hang: it terminates with an error and
// emits no deadlock event.
func TestDeadlockDetectionDisabledTerminatesWithError(t *testing.T) {
	sp := scenario.DeadlockAB()
	off := false
	sp.Options.DeadlockDetection = &off
	res, err := scheduler.Run(sp, nil, nil)
	if err != nil {
		t.Fatalf("unexpected Run error: %v", err)
	}
	if res.Deadlock != nil {
		t.Errorf("expected no deadlock info when detection disabled, got %+v", res.Deadlock)
	}
	if res.Error == "" {
		t.Error("expected termination error for undetected cycle, got empty")
	}
	for _, e := range res.Events {
		if e.Kind == event.Deadlock {
			t.Error("deadlock event emitted despite disabled detection")
		}
	}
}

// TestPreemptionByArrival is the baseline: a newly arriving higher-priority
// task preempts the running task immediately.
func TestPreemptionByArrival(t *testing.T) {
	sp := scheduler.Spec{
		Name:      "basic-preemption",
		Resources: nil,
		Tasks: []scheduler.TaskSpec{
			{ID: "Lo", Arrival: 0, Priority: 1,
				Program: program.NewScript(program.Action{CPU: 5})},
			{ID: "Hi", Arrival: 2, Priority: 9,
				Program: program.NewScript(program.Action{CPU: 1})},
		},
	}
	res := runSpec(t, sp)
	// First preemption event must name Hi at exactly tick 2.
	var preAt int64 = -1
	for _, e := range res.Events {
		if e.Kind == event.TaskPreempted && e.Task == "Lo" {
			preAt = e.Time
			break
		}
	}
	if preAt != 2 {
		t.Errorf("preemption at tick %d, want 2", preAt)
	}
	if res.Summary.CompletedAt["Hi"] != 3 {
		t.Errorf("Hi completed at %d, want 3", res.Summary.CompletedAt["Hi"])
	}
}

// TestValidationErrors ensures malformed specs are rejected rather than
// producing nonsense timelines.
func TestValidationErrors(t *testing.T) {
	bad := []scheduler.Spec{
		{Name: "no tasks"},
		{
			Name: "dup tasks",
			Tasks: []scheduler.TaskSpec{
				{ID: "X", Program: program.NewScript(program.Action{CPU: 1})},
				{ID: "X", Program: program.NewScript(program.Action{CPU: 1})},
			},
		},
		{
			Name:      "undeclared resource",
			Resources: []string{"R"},
			Tasks: []scheduler.TaskSpec{
				{ID: "X", Program: program.NewScript(program.Action{Acquire: "Q"})},
			},
		},
		{
			Name: "bad action",
			Tasks: []scheduler.TaskSpec{
				{ID: "X", Program: &program.ScriptProgram{Actions: []program.Action{{}}}},
			},
		},
	}
	for _, sp := range bad {
		if _, err := scheduler.Run(sp, nil, nil); err == nil {
			t.Errorf("spec %q: expected validation error, got nil", sp.Name)
		}
	}
}

// TestProgramErrorReleaseNotOwned checks runtime misuse (releasing a lock the
// task never acquired) is reported as a structured program_error event.
func TestProgramErrorReleaseNotOwned(t *testing.T) {
	sp := scheduler.Spec{
		Name:      "bad-unlock",
		Resources: []string{"R"},
		Tasks: []scheduler.TaskSpec{
			{ID: "X", Priority: 1,
				Program: program.NewScript(program.Action{Release: "R"})},
		},
	}
	res := runSpec(t, sp)
	if res.Error == "" {
		t.Fatal("expected error string on Result")
	}
	var saw bool
	for _, e := range res.Events {
		if e.Kind == event.ProgramError {
			saw = true
		}
	}
	if !saw {
		t.Error("no program_error event emitted")
	}
}

// TestDeterminism verifies two runs of the same spec produce byte-identical
// event sequences.
func TestDeterminism(t *testing.T) {
	a := runSpec(t, scenario.ThreeLevelInheritance())
	b := runSpec(t, scenario.ThreeLevelInheritance())
	if len(a.Events) != len(b.Events) {
		t.Fatalf("event count differs: %d vs %d", len(a.Events), len(b.Events))
	}
	for i := range a.Events {
		if !reflect.DeepEqual(a.Events[i], b.Events[i]) {
			t.Fatalf("event %d differs:\n%+v\n%+v", i, a.Events[i], b.Events[i])
		}
	}
}

// TestEventSequenceMonotonic asserts seq is dense and zero-based and times
// never go backwards.
func TestEventSequenceMonotonic(t *testing.T) {
	res := runSpec(t, scenario.MultiLockReleaseOrder("R2-first"))
	var lastTime int64
	for i, e := range res.Events {
		if e.Seq != int64(i) {
			t.Fatalf("event %d has seq %d", i, e.Seq)
		}
		if e.Time < lastTime {
			t.Fatalf("time went backwards: %d -> %d", lastTime, e.Time)
		}
		lastTime = e.Time
	}
}

func equalPairs(a, b [][2]int) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
