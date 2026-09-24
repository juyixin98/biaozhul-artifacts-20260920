package sim

import (
	"reflect"
	"strings"
	"testing"
)

func blockedOf(r *Result, task string) TaskMetrics {
	return r.Metrics[task]
}

func eventTypes(r *Result) []string {
	types := make([]string, len(r.Events))
	for i, ev := range r.Events {
		types[i] = ev.Type
	}
	return types
}

func findEvent(r *Result, typ, task string) (Event, bool) {
	for _, ev := range r.Events {
		if ev.Type == typ && ev.Task == task {
			return ev, true
		}
	}
	return Event{}, false
}

// TestBasicInversion: medium task interposes while High waits on Low's lock.
// Without PIP High is blocked for 8 ticks; with PIP for 5.
func TestBasicInversion(t *testing.T) {
	wl, _ := Preset("basic")

	off, err := Execute(wl, false)
	if err != nil {
		t.Fatalf("execute off: %v", err)
	}
	on, err := Execute(wl, true)
	if err != nil {
		t.Fatalf("execute on: %v", err)
	}

	if got := blockedOf(off, "High").BlockedTicks; got != 7 {
		t.Errorf("High blocked without PIP = %d, want 7", got)
	}
	if got := blockedOf(on, "High").BlockedTicks; got != 3 {
		t.Errorf("High blocked with PIP = %d, want 3", got)
	}
	// Medium's compute segment must actually run between High's block and grant.
	var mediumRanDuringBlock bool
	blockedAt, grantedAt := -1, -1
	for _, ev := range on.Events {
		if ev.Type == "lock-blocked" && ev.Task == "High" {
			blockedAt = ev.Tick
		}
		if ev.Type == "lock-granted" && ev.Task == "High" {
			grantedAt = ev.Tick
		}
		if ev.Type == "tick" && ev.Task == "Medium" && blockedAt >= 0 && grantedAt < 0 {
			mediumRanDuringBlock = true
		}
	}
	if mediumRanDuringBlock {
		t.Errorf("with PIP, Medium must not run while High is blocked on the lock")
	}
	if blockedAt != 3 || grantedAt != 6 {
		t.Errorf("block/grant timing = %d/%d, want 3/6", blockedAt, grantedAt)
	}
	// Completion order changes: High finishes before Medium only with PIP.
	if strings.Join(on.CompletionOrder, ",") != "High,Medium,Low" {
		t.Errorf("completion order with PIP = %v, want High,Medium,Low", on.CompletionOrder)
	}
	if strings.Join(off.CompletionOrder, ",") != "Medium,High,Low" {
		t.Errorf("completion order without PIP = %v, want Medium,High,Low", off.CompletionOrder)
	}
}

// TestNestedChain: effective priority must propagate High -> Medium -> Low.
func TestNestedChain(t *testing.T) {
	wl, _ := Preset("nested")
	off, err := Execute(wl, false)
	if err != nil {
		t.Fatalf("execute off: %v", err)
	}
	on, err := Execute(wl, true)
	if err != nil {
		t.Fatalf("execute on: %v", err)
	}

	if got := blockedOf(on, "Busy").BlockedTicks; got != 0 {
		t.Errorf("Busy should never wait on a lock, got %d", got)
	}
	if got := blockedOf(off, "High").BlockedTicks; got != 11 {
		t.Errorf("High blocked without PIP = %d, want 11", got)
	}
	if got := blockedOf(on, "High").BlockedTicks; got != 7 {
		t.Errorf("High blocked with PIP = %d, want 7", got)
	}

	// Transitive inheritance: Low must be boosted directly to High's priority 30.
	ev, ok := findEvent(on, "priority", "Low")
	if !ok {
		t.Fatalf("expected a priority boost event for Low")
	}
	if ev.FromPrio == nil || *ev.FromPrio != 10 {
		t.Errorf("Low boost from = %v, want 10", ev.FromPrio)
	}
	if ev.ToPrio == nil || *ev.ToPrio != 30 {
		t.Errorf("Low boost to = %v, want 30 (transitive through Medium)", ev.ToPrio)
	}
	if !strings.Contains(ev.Reason, "High") || !strings.Contains(ev.Reason, "Medium") {
		t.Errorf("boost reason should name the High -> Medium -> Low chain, got %q", ev.Reason)
	}

	// Without PIP there must be no boost events at all.
	for _, e := range off.Events {
		if e.Type == "priority" {
			t.Errorf("unexpected priority event without inheritance: %+v", e)
		}
	}

	// A compare run reproduces the same numbers.
	rep, err := Compare(wl)
	if err != nil {
		t.Fatalf("compare: %v", err)
	}
	var highDelta int
	for _, c := range rep.Tasks {
		if c.ID == "High" {
			highDelta = c.BlockedDelta
		}
	}
	if highDelta != 4 {
		t.Errorf("High blocked delta = %d, want 4", highDelta)
	}
}

// TestPriorityRestore: when the last inherited lock is released the owner
// returns to its base priority before later ops execute.
func TestPriorityRestore(t *testing.T) {
	wl := Workload{
		Resources: []string{"R"},
		Tasks: []TaskSpec{
			{ID: "Low", BasePriority: 10, Release: 0, Ops: []Op{
				{Kind: OpLock, Resource: "R"},
				{Kind: OpCompute, Duration: 2},
				{Kind: OpUnlock, Resource: "R"},
				{Kind: OpCompute, Duration: 2},
			}},
			{ID: "Mid", BasePriority: 20, Release: 1, Ops: []Op{
				{Kind: OpCompute, Duration: 4},
			}},
			{ID: "High", BasePriority: 30, Release: 1, Ops: []Op{
				{Kind: OpLock, Resource: "R"},
				{Kind: OpCompute, Duration: 1},
				{Kind: OpUnlock, Resource: "R"},
			}},
		},
	}
	on, err := Execute(wl, true)
	if err != nil {
		t.Fatalf("execute: %v", err)
	}
	var sawRestore bool
	for _, ev := range on.Events {
		if ev.Type == "priority" && ev.Task == "Low" &&
			ev.FromPrio != nil && *ev.FromPrio == 30 &&
			ev.ToPrio != nil && *ev.ToPrio == 10 {
			sawRestore = true
		}
	}
	if !sawRestore {
		t.Errorf("expected Low to be restored 30 -> 10 after releasing R")
	}
	// After restore Mid (20) preempts Low for its remaining work.
	order := strings.Join(on.CompletionOrder, ",")
	if order != "High,Mid,Low" {
		t.Errorf("completion order = %q, want High,Mid,Low", order)
	}
}

// TestHighestWaiterGrant: release grants the lock to the highest-priority
// waiter, even though another task queued earlier.
func TestHighestWaiterGrant(t *testing.T) {
	wl := Workload{
		Resources: []string{"R"},
		Tasks: []TaskSpec{
			{ID: "Owner", BasePriority: 10, Release: 0, Ops: []Op{
				{Kind: OpLock, Resource: "R"},
				{Kind: OpCompute, Duration: 3},
				{Kind: OpUnlock, Resource: "R"},
			}},
			{ID: "W1", BasePriority: 15, Release: 1, Ops: []Op{
				{Kind: OpLock, Resource: "R"},
				{Kind: OpCompute, Duration: 1},
				{Kind: OpUnlock, Resource: "R"},
			}},
			{ID: "W2", BasePriority: 25, Release: 2, Ops: []Op{
				{Kind: OpLock, Resource: "R"},
				{Kind: OpCompute, Duration: 1},
				{Kind: OpUnlock, Resource: "R"},
			}},
		},
	}
	res, err := Execute(wl, true)
	if err != nil {
		t.Fatalf("execute: %v", err)
	}
	ev, ok := findEvent(res, "lock-granted", "W2")
	if !ok {
		t.Fatalf("expected W2 to be granted despite queuing after W1")
	}
	if ev.Tick != 3 {
		t.Errorf("grant tick = %d, want 3", ev.Tick)
	}
	if order := strings.Join(res.CompletionOrder, ","); order != "Owner,W2,W1" {
		t.Errorf("completion order = %q, want Owner,W2,W1", order)
	}
}

func TestDeterminism(t *testing.T) {
	wl, _ := Preset("nested")
	a, err := Execute(wl, true)
	if err != nil {
		t.Fatalf("execute: %v", err)
	}
	for i := 0; i < 5; i++ {
		b, err := Execute(wl, true)
		if err != nil {
			t.Fatalf("execute repeat %d: %v", i, err)
		}
		if !reflect.DeepEqual(a.Events, b.Events) {
			t.Fatalf("run %d produced a different event trace", i)
		}
	}
}

// TestOrderIndependence: declaring tasks in a different order must not change
// the per-task scheduling outcome (only event ordering among same-instant
// releases may differ). Compares key metrics and the boost chain.
func TestOrderIndependence(t *testing.T) {
	wl, _ := Preset("nested")
	rev := wl
	rev.Tasks = append([]TaskSpec(nil), wl.Tasks...)
	for i, j := 0, len(rev.Tasks)-1; i < j; i, j = i+1, j-1 {
		rev.Tasks[i], rev.Tasks[j] = rev.Tasks[j], rev.Tasks[i]
	}
	a, err := Execute(wl, true)
	if err != nil {
		t.Fatalf("execute: %v", err)
	}
	b, err := Execute(rev, true)
	if err != nil {
		t.Fatalf("execute reversed: %v", err)
	}
	for _, id := range []string{"Low", "Medium", "High", "Busy"} {
		ma, mb := a.Metrics[id], b.Metrics[id]
		if ma.StartedAt != mb.StartedAt || ma.FinishedAt != mb.FinishedAt ||
			ma.BlockedTicks != mb.BlockedTicks {
			t.Errorf("task %s metrics differ under reordering: %+v vs %+v", id, ma, mb)
		}
	}
	ea, _ := findEvent(a, "priority", "Low")
	eb, _ := findEvent(b, "priority", "Low")
	if ea.ToPrio == nil || eb.ToPrio == nil || *ea.ToPrio != *eb.ToPrio {
		t.Errorf("Low's transitive boost changed under reordering: %v vs %v",
			ea.ToPrio, eb.ToPrio)
	}
}

func TestDeadlock(t *testing.T) {
	wl := Workload{
		Resources: []string{"A", "B"},
		Tasks: []TaskSpec{
			{ID: "T1", BasePriority: 10, Release: 0, Ops: []Op{
				{Kind: OpLock, Resource: "A"},
				{Kind: OpCompute, Duration: 1},
				{Kind: OpLock, Resource: "B"},
				{Kind: OpCompute, Duration: 1},
				{Kind: OpUnlock, Resource: "B"},
				{Kind: OpUnlock, Resource: "A"},
			}},
			{ID: "T2", BasePriority: 20, Release: 0, Ops: []Op{
				{Kind: OpLock, Resource: "B"},
				{Kind: OpCompute, Duration: 2},
				{Kind: OpLock, Resource: "A"},
				{Kind: OpCompute, Duration: 1},
				{Kind: OpUnlock, Resource: "A"},
				{Kind: OpUnlock, Resource: "B"},
			}},
		},
	}
	res, err := Execute(wl, true)
	if err != nil {
		t.Fatalf("deadlock should be reported via Result, not error: %v", err)
	}
	if !res.Deadlocked {
		t.Fatalf("expected deadlock detection")
	}
	if len(res.DeadlockCycle) < 3 {
		t.Errorf("expected cycle with at least 3 nodes, got %v", res.DeadlockCycle)
	}
	if res.DeadlockCycle[0] != res.DeadlockCycle[len(res.DeadlockCycle)-1] {
		t.Errorf("cycle should close: %v", res.DeadlockCycle)
	}
	if _, ok := findEvent(res, "deadlock", ""); !ok {
		t.Errorf("expected deadlock event")
	}
}

func TestIdleAdvance(t *testing.T) {
	wl := Workload{
		Resources: []string{"R"},
		Tasks: []TaskSpec{
			{ID: "Late", BasePriority: 10, Release: 10, Ops: []Op{
				{Kind: OpCompute, Duration: 1},
			}},
		},
	}
	res, err := Execute(wl, false)
	if err != nil {
		t.Fatalf("execute: %v", err)
	}
	ev, ok := findEvent(res, "idle", "")
	if !ok {
		t.Fatalf("expected idle jump before first release")
	}
	if ev.FromTick == nil || *ev.FromTick != 0 || ev.NextTick == nil || *ev.NextTick != 10 {
		t.Errorf("idle jump = %v -> %v, want 0 -> 10", ev.FromTick, ev.NextTick)
	}
	if res.Ticks != 11 {
		t.Errorf("finished at %d, want 11", res.Ticks)
	}
}

func TestFinishesOnZeroOp(t *testing.T) {
	wl := Workload{
		Resources: []string{"R"},
		Tasks: []TaskSpec{
			{ID: "T", BasePriority: 10, Release: 0, Ops: []Op{
				{Kind: OpCompute, Duration: 1},
				{Kind: OpLock, Resource: "R"},
				{Kind: OpUnlock, Resource: "R"},
			}},
		},
	}
	res, err := Execute(wl, true)
	if err != nil {
		t.Fatalf("execute: %v", err)
	}
	if m := res.Metrics["T"]; m.FinishedAt != 1 {
		t.Errorf("finished at %d, want 1", m.FinishedAt)
	}
}

func TestValidationErrors(t *testing.T) {
	base := func() Workload {
		return Workload{
			Resources: []string{"R"},
			Tasks: []TaskSpec{
				{ID: "T", BasePriority: 10, Release: 0, Ops: []Op{
					{Kind: OpLock, Resource: "R"},
					{Kind: OpCompute, Duration: 1},
					{Kind: OpUnlock, Resource: "R"},
				}},
			},
		}
	}
	cases := []struct {
		name   string
		mutate func(*Workload)
	}{
		{"no tasks", func(w *Workload) { w.Tasks = nil }},
		{"empty id", func(w *Workload) { w.Tasks[0].ID = "" }},
		{"duplicate id", func(w *Workload) {
			w.Tasks = append(w.Tasks, TaskSpec{ID: "T", Ops: []Op{{Kind: OpCompute, Duration: 1}}})
		}},
		{"negative release", func(w *Workload) { w.Tasks[0].Release = -1 }},
		{"zero compute", func(w *Workload) {
			w.Tasks[0].Ops = []Op{{Kind: OpCompute, Duration: 0}}
		}},
		{"unknown resource", func(w *Workload) {
			w.Tasks[0].Ops[0].Resource = "X"
		}},
		{"unlock not held", func(w *Workload) {
			w.Tasks[0].Ops = []Op{
				{Kind: OpLock, Resource: "R"},
				{Kind: OpCompute, Duration: 1},
				{Kind: OpUnlock, Resource: "R"},
				{Kind: OpUnlock, Resource: "R"},
			}
		}},
		{"still holding at end", func(w *Workload) {
			w.Tasks[0].Ops = []Op{
				{Kind: OpLock, Resource: "R"},
				{Kind: OpCompute, Duration: 1},
			}
		}},
		{"unknown op", func(w *Workload) {
			w.Tasks[0].Ops = []Op{{Kind: "yolo", Duration: 1}}
		}},
		{"duplicate resource", func(w *Workload) { w.Resources = []string{"R", "R"} }},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			w := base()
			tc.mutate(&w)
			if _, err := Execute(w, true); err == nil {
				t.Errorf("expected validation error for %s", tc.name)
			}
		})
	}
}

func TestMaxTicks(t *testing.T) {
	wl := Workload{
		MaxTicks:  3,
		Resources: []string{"R"},
		Tasks: []TaskSpec{
			{ID: "T", BasePriority: 10, Release: 0, Ops: []Op{
				{Kind: OpLock, Resource: "R"},
				{Kind: OpCompute, Duration: 10},
				{Kind: OpUnlock, Resource: "R"},
			}},
		},
	}
	if _, err := Execute(wl, true); err == nil {
		t.Errorf("expected max_ticks error")
	}
}
