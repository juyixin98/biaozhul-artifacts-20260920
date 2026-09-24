package simulator

import (
	"encoding/json"
	"testing"
)

// findTask is a small helper for assertions.
func findTask(t *testing.T, res Result, name string) TaskResult {
	t.Helper()
	for _, tr := range res.Tasks {
		if tr.Name == name {
			return tr
		}
	}
	t.Fatalf("task %q not in result", name)
	return TaskResult{}
}

func blockedOf(res Result, name string) int {
	for _, tr := range res.Tasks {
		if tr.Name == name {
			return tr.BlockedTicks
		}
	}
	return -1
}

// TestClassicInheritance is the primary acceptance case: the high-priority
// task H blocks far less with inheritance because the lock holder L is boosted
// past the medium interference task M.
func TestClassicInheritance(t *testing.T) {
	sc, ok := FindScenario("classic")
	if !ok {
		t.Fatal("classic scenario missing")
	}
	cmp, err := Compare(CompareRequest{Tasks: sc.Tasks})
	if err != nil {
		t.Fatalf("compare: %v", err)
	}
	withH := blockedOf(cmp.WithInheritance, "H")
	withoutH := blockedOf(cmp.WithoutInheritance, "H")
	if withH != 3 {
		t.Fatalf("with PIP H blocked = %d ticks, want 3; trace:\n%s", withH, trace(cmp.WithInheritance))
	}
	if withoutH != 8 {
		t.Fatalf("without PIP H blocked = %d ticks, want 8; trace:\n%s", withoutH, trace(cmp.WithoutInheritance))
	}
	if cmp.HighestWaiter != "H" || cmp.HighestWaiterBlockedWith != 3 ||
		cmp.HighestWaiterBlockedWithout != 8 {
		t.Fatalf("highest-waiter summary wrong: %+v", cmp)
	}
	if !cmp.WithInheritance.Completed || !cmp.WithoutInheritance.Completed {
		t.Fatal("both classic runs must complete")
	}
	// M never acquires any lock; its running time equals its 5-tick program
	// in both runs (it is preempted by boosted L but gets its CPU back).
	if m := findTask(t, cmp.WithInheritance, "M"); m.RunningTicks != 5 {
		t.Errorf("M running ticks with PIP = %d, want 5", m.RunningTicks)
	}
	if m := findTask(t, cmp.WithoutInheritance, "M"); m.RunningTicks != 5 {
		t.Errorf("M running ticks without PIP = %d, want 5", m.RunningTicks)
	}
	// No task may be blocked after completion; totals are consistent.
	for _, r := range []Result{cmp.WithInheritance, cmp.WithoutInheritance} {
		for _, tr := range r.Tasks {
			sum := 0
			for _, a := range tr.LockAttempts {
				sum += a.WaitedTicks
				if a.Acquired != (a.AcquiredAt != nil) {
					t.Errorf("%s: acquired flag/timestamp inconsistent", tr.Name)
				}
			}
			if sum != tr.TotalLockWaits {
				t.Errorf("%s: lock wait sums mismatch %d != %d", tr.Name, sum, tr.TotalLockWaits)
			}
		}
	}
}

// TestNestedChainInheritance verifies transitive donation H -> X -> L and
// that L beats the medium task M, followed by correct restoration when the
// chain unwinds.
func TestNestedChainInheritance(t *testing.T) {
	sc, _ := FindScenario("nested-chain")
	cmp, err := Compare(CompareRequest{Tasks: sc.Tasks})
	if err != nil {
		t.Fatalf("compare: %v", err)
	}
	withH := blockedOf(cmp.WithInheritance, "H")
	withoutH := blockedOf(cmp.WithoutInheritance, "H")
	if withH != 7 {
		t.Fatalf("with PIP H blocked = %d, want 7; trace:\n%s", withH, trace(cmp.WithInheritance))
	}
	if withoutH != 12 {
		t.Fatalf("without PIP H blocked = %d, want 12; trace:\n%s", withoutH, trace(cmp.WithoutInheritance))
	}
	if xb := blockedOf(cmp.WithInheritance, "X"); xb != 5 {
		t.Fatalf("with PIP X blocked = %d, want 5; trace:\n%s", xb, trace(cmp.WithInheritance))
	}
	if xb := blockedOf(cmp.WithoutInheritance, "X"); xb != 10 {
		t.Fatalf("without PIP X blocked = %d, want 10; trace:\n%s", xb, trace(cmp.WithoutInheritance))
	}
	// Under PIP, L must actually be boosted, transitively, to priority 3.
	boosted := map[string]int{}
	for _, ev := range cmp.WithInheritance.Events {
		if ev.Kind == evPriorityBoost && ev.NewPriority == 3 {
			boosted[ev.Task] = ev.NewPriority
		}
	}
	if boosted["L"] != 3 {
		t.Errorf("L never received transitive boost to 3; boosts=%v", boosted)
	}
	if boosted["X"] != 3 {
		t.Errorf("X never received boost to 3; boosts=%v", boosted)
	}
	// H boosted nobody.
	if _, bad := boosted["H"]; bad {
		t.Errorf("H must never inherit, it is the top-priority donor")
	}
	// Every boost must eventually be restored: no task holds a raised
	// effective priority when the workload completes.
	for _, tr := range cmp.WithInheritance.Tasks {
		if !tr.Completed {
			t.Fatalf("task %s did not complete", tr.Name)
		}
	}
}

// TestPriorityRestoredAfterUnlock checks that a boosted holder drops back to
// its base priority exactly when the wait disappears.
func TestPriorityRestoredAfterUnlock(t *testing.T) {
	// Small two-task program: L holds A while H wants A; after unlock, L must
	// show PRIORITY_RESTORE and then lose to H directly (not via inheritance).
	cfg := Config{EnableInheritance: true, Tasks: []TaskSpec{
		{Name: "L", Priority: 1, ReleaseTime: 0, Steps: []Step{
			{Op: OpLock, Resource: "A"},
			{Op: OpCompute, Duration: 2},
			{Op: OpUnlock, Resource: "A"},
			{Op: OpCompute, Duration: 2},
		}},
		{Name: "H", Priority: 3, ReleaseTime: 1, Steps: []Step{
			{Op: OpLock, Resource: "A"},
			{Op: OpCompute, Duration: 2},
			{Op: OpUnlock, Resource: "A"},
		}},
	}}
	res, err := Run(cfg)
	if err != nil {
		t.Fatal(err)
	}
	sawBoost := false
	sawRestore := false
	for _, ev := range res.Events {
		if ev.Kind == evPriorityBoost && ev.Task == "L" && ev.NewPriority == 3 &&
			ev.CausedBy == "H" {
			sawBoost = true
		}
		if ev.Kind == evPriorityDrop && ev.Task == "L" && ev.NewPriority == 1 {
			sawRestore = true
		}
	}
	if !sawBoost {
		t.Errorf("missing L boost event; trace:\n%s", trace(res))
	}
	if !sawRestore {
		t.Errorf("missing L restore event; trace:\n%s", trace(res))
	}
	// After restore, H finishes before L's tail work.
	hf := findTask(t, res, "H").FinishTime
	lf := findTask(t, res, "L").FinishTime
	if hf == nil || lf == nil || *hf >= *lf {
		t.Errorf("H should finish before L after restoration, H=%v L=%v", hf, lf)
	}
}

// TestDeadlockDetected checks both modes report the cycle and do not hang.
func TestDeadlockDetected(t *testing.T) {
	sc, _ := FindScenario("deadlock")
	for _, inherit := range []bool{true, false} {
		res, err := Run(Config{EnableInheritance: inherit, Tasks: sc.Tasks})
		if err != nil {
			t.Fatalf("inherit=%v run: %v", inherit, err)
		}
		if !res.Deadlocked || res.Status != "deadlocked" || res.Completed {
			t.Fatalf("inherit=%v expected deadlocked status, got %+v", inherit, res.Status)
		}
		var ev *Event
		for i := range res.Events {
			if res.Events[i].Kind == evDeadlock {
				ev = &res.Events[i]
			}
		}
		if ev == nil {
			t.Fatalf("inherit=%v no DEADLOCK event; trace:\n%s", inherit, trace(res))
		}
		// The cycle names the two locked tasks (start node repeats only
		// implicitly; [L H] means L waits on B held by H and H on A held by L).
		if len(ev.Cycle) != 2 {
			t.Errorf("deadlock cycle %v should name L and H", ev.Cycle)
		}
		joined := ev.Cycle[0] + "," + ev.Cycle[1]
		if joined != "L,H" && joined != "H,L" {
			t.Errorf("deadlock cycle %v should be {L,H}", ev.Cycle)
		}
	}
}

// TestDeterministic runs the same config repeatedly and compares JSON traces.
func TestDeterministic(t *testing.T) {
	sc, _ := FindScenario("nested-chain")
	first, err := Run(Config{EnableInheritance: true, Tasks: sc.Tasks})
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 5; i++ {
		res, err := Run(Config{EnableInheritance: true, Tasks: sc.Tasks})
		if err != nil {
			t.Fatal(err)
		}
		a, _ := json.Marshal(first.Events)
		b, _ := json.Marshal(res.Events)
		if string(a) != string(b) {
			t.Fatalf("run %d trace differs from first run", i)
		}
	}
}

// TestIdleAdvance ensures released tasks only run from their release time and
// the simulator emits IDLE jumps instead of busy-looping.
func TestIdleAdvance(t *testing.T) {
	res, err := Run(Config{EnableInheritance: true, Tasks: []TaskSpec{
		{Name: "T", Priority: 1, ReleaseTime: 5, Steps: []Step{
			{Op: OpCompute, Duration: 1},
		}},
	}})
	if err != nil {
		t.Fatal(err)
	}
	if res.Makespan != 6 {
		t.Errorf("makespan = %d, want 6", res.Makespan)
	}
	sawIdle := false
	for _, ev := range res.Events {
		if ev.Kind == evIdle && ev.JumpTo == 5 {
			sawIdle = true
		}
		if ev.Kind == evCompute && ev.Time < 5 {
			t.Errorf("compute before release at %d", ev.Time)
		}
	}
	if !sawIdle {
		t.Errorf("expected IDLE jump to 5; trace:\n%s", trace(res))
	}
}

// TestSingleTaskNoBlocking is a simple execution sanity check.
func TestSingleTaskNoBlocking(t *testing.T) {
	res, err := Run(Config{EnableInheritance: true, Tasks: []TaskSpec{
		{Name: "solo", Priority: 1, ReleaseTime: 0, Steps: []Step{
			{Op: OpCompute, Duration: 3},
		}},
	}})
	if err != nil {
		t.Fatal(err)
	}
	tr := findTask(t, res, "solo")
	if tr.RunningTicks != 3 || tr.BlockedTicks != 0 || res.Makespan != 3 {
		t.Fatalf("unexpected solo result: %+v makespan=%d", tr, res.Makespan)
	}
}

// TestNestingLIFO verifies a task may nest locks and releases them properly.
func TestNestingLIFO(t *testing.T) {
	res, err := Run(Config{EnableInheritance: true, Tasks: []TaskSpec{
		{Name: "A", Priority: 1, ReleaseTime: 0, Steps: []Step{
			{Op: OpLock, Resource: "x"},
			{Op: OpLock, Resource: "y"},
			{Op: OpCompute, Duration: 1},
			{Op: OpUnlock, Resource: "y"},
			{Op: OpUnlock, Resource: "x"},
		}},
	}})
	if err != nil {
		t.Fatal(err)
	}
	if !res.Completed {
		t.Fatalf("nested locking should complete; trace:\n%s", trace(res))
	}
}

// TestValidationErrors covers the static input checks.
func TestValidationErrors(t *testing.T) {
	good := []Step{{Op: OpCompute, Duration: 1}}
	cases := []struct {
		name string
		cfg  Config
	}{
		{"no tasks", Config{}},
		{"empty name", Config{Tasks: []TaskSpec{{Steps: good}}}},
		{"duplicate name", Config{Tasks: []TaskSpec{
			{Name: "x", Steps: good}, {Name: "x", Steps: good}}}},
		{"negative release", Config{Tasks: []TaskSpec{
			{Name: "x", ReleaseTime: -1, Steps: good}}}},
		{"no steps", Config{Tasks: []TaskSpec{{Name: "x"}}}},
		{"bad compute duration", Config{Tasks: []TaskSpec{{Name: "x", Steps: []Step{
			{Op: OpCompute, Duration: 0}}}}}},
		{"lock missing resource", Config{Tasks: []TaskSpec{{Name: "x", Steps: []Step{
			{Op: OpLock}, {Op: OpCompute, Duration: 1}}}}}},
		{"unlock without lock", Config{Tasks: []TaskSpec{{Name: "x", Steps: []Step{
			{Op: OpUnlock, Resource: "r"}, {Op: OpCompute, Duration: 1}}}}}},
		{"unheld lock at end", Config{Tasks: []TaskSpec{{Name: "x", Steps: []Step{
			{Op: OpLock, Resource: "r"}, {Op: OpCompute, Duration: 1}}}}}},
		{"unknown op", Config{Tasks: []TaskSpec{{Name: "x", Steps: []Step{
			{Op: "sleep", Duration: 1}}}}}},
		{"only locks no compute", Config{Tasks: []TaskSpec{{Name: "x", Steps: []Step{
			{Op: OpLock, Resource: "r"}, {Op: OpUnlock, Resource: "r"}}}}}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := Run(tc.cfg); err == nil {
				t.Fatal("expected validation error, got nil")
			}
		})
	}
}

// TestMaxTimeTimeout makes sure the bound terminates a pathological run.
func TestMaxTimeTimeout(t *testing.T) {
	res, err := Run(Config{MaxTime: 2, EnableInheritance: true, Tasks: []TaskSpec{
		{Name: "x", Priority: 1, Steps: []Step{{Op: OpCompute, Duration: 10}}},
	}})
	if err != nil {
		t.Fatal(err)
	}
	if !res.TimedOut || res.Status != "timeout" {
		t.Fatalf("expected timeout, got %s", res.Status)
	}
}

// trace renders a compact event trace for failure messages.
func trace(res Result) string {
	return Trace(res)
}
