package scheduler

import (
	"strings"
	"testing"
)

// helpers --------------------------------------------------------------------

func findTask(r *Report, id string) TaskReport {
	for _, t := range r.Tasks {
		if t.ID == id {
			return t
		}
	}
	return TaskReport{}
}

func types(r *Report) []EventType {
	out := make([]EventType, 0, len(r.Events))
	for _, e := range r.Events {
		out = append(out, e.Type)
	}
	return out
}

func countType(r *Report, ty EventType) int {
	n := 0
	for _, e := range r.Events {
		if e.Type == ty {
			n++
		}
	}
	return n
}

func effAt(r *Report, id string) int { return findTask(r, id).Effective }

// lastBoostFor returns the final effective priority recorded in boost/reset
// events for the task.
func finalEventEff(r *Report, id string) (int, bool) {
	eff := 0
	found := false
	for _, e := range r.Events {
		if (e.Type == EvPriorityBoost || e.Type == EvPriorityReset) && e.Task == id {
			eff = e.New
			found = true
		}
	}
	return eff, found
}

// TestBasicPreemption: a later high-priority task preempts a running low one.
func TestBasicPreemption(t *testing.T) {
	cfg := Config{
		Locks: []LockSpec{{ID: "A"}},
		Tasks: []TaskSpec{
			{ID: "low", Base: 1, Arrival: 0, Program: Program{{Op: OpCPU, Ticks: 5}}},
			{ID: "high", Base: 9, Arrival: 1, Program: Program{{Op: OpCPU, Ticks: 2}}},
		},
	}
	s, err := New(cfg)
	if err != nil {
		t.Fatal(err)
	}
	r := s.Run()
	if !r.Completed {
		t.Fatalf("expected completion: fatal=%q deadlocked=%v", r.Fatal, r.Deadlocked)
	}
	high := findTask(r, "high")
	low := findTask(r, "low")
	// high arrives at 1, runs 1-2, finishes at 3 (its 2 ticks are 1->2, 2->3).
	if high.FinishTick != 3 {
		t.Errorf("high finish = %d, want 3", high.FinishTick)
	}
	if low.FinishTick != 7 {
		t.Errorf("low finish = %d, want 7", low.FinishTick)
	}
	if countType(r, EvPreempt) != 1 {
		t.Errorf("want exactly 1 preemption, got %d", countType(r, EvPreempt))
	}
	if low.ReadyTicks < 2 {
		t.Errorf("low should have waited ~2 ready ticks, got %d", low.ReadyTicks)
	}
}

// TestInheritanceBoost verifies that a lock holder inherits the waiter's
// priority and loses it again after release.
func TestInheritanceBoost(t *testing.T) {
	cfg := Config{
		Locks: []LockSpec{{ID: "A"}},
		Tasks: []TaskSpec{
			{ID: "L", Base: 1, Arrival: 0, Program: Program{
				{Op: OpCPU, Ticks: 1},
				{Op: OpLock, Lock: "A"},
				{Op: OpCPU, Ticks: 4},
				{Op: OpUnlock, Lock: "A"},
				{Op: OpCPU, Ticks: 1},
			}},
			{ID: "M", Base: 5, Arrival: 2, Program: Program{{Op: OpCPU, Ticks: 6}}},
			{ID: "H", Base: 9, Arrival: 3, Program: Program{
				{Op: OpCPU, Ticks: 1},
				{Op: OpLock, Lock: "A"},
				{Op: OpCPU, Ticks: 1},
				{Op: OpUnlock, Lock: "A"},
			}},
		},
	}
	s, _ := New(cfg)
	r := s.Run()
	if !r.Completed {
		t.Fatalf("not completed: %v %v", r.Deadlocked, r.Fatal)
	}
	l := findTask(r, "L")
	if l.PriorityBoosts < 1 {
		t.Fatalf("L should have been boosted, boosts=%d", l.PriorityBoosts)
	}
	// L's final effective priority must be back to its base.
	if l.Effective != 1 {
		t.Errorf("L final eff = %d, want 1 (reset to base)", l.Effective)
	}
	// While holding the lock, L must have run with eff 9: check a boost event.
	saw9 := false
	for _, e := range r.Events {
		if e.Type == EvPriorityBoost && e.Task == "L" && e.New == 9 {
			saw9 = true
		}
	}
	if !saw9 {
		t.Error("L never boosted to 9")
	}
	// While holding A after H blocks at t=4, L runs at inherited priority 9,
	// finishing the critical section before M can run.
	for _, e := range r.Events {
		if e.Type == EvCPUTick && e.Task == "L" && e.Tick >= 4 && e.Tick < 7 {
			if e.Effective != 9 {
				t.Errorf("L ran at tick %d with eff %d, want 9", e.Tick, e.Effective)
			}
		}
	}
}

// TestNoInversionWithoutPIP reproduces the inversion in "none" mode and
// confirms effective priorities never change.
func TestNoInversionWithoutPIP(t *testing.T) {
	cfg := Config{
		Inheritance: InheritanceNone,
		Locks:       []LockSpec{{ID: "A"}},
		Tasks: []TaskSpec{
			{ID: "L", Base: 1, Arrival: 0, Program: Program{
				{Op: OpCPU, Ticks: 1},
				{Op: OpLock, Lock: "A"},
				{Op: OpCPU, Ticks: 4},
				{Op: OpUnlock, Lock: "A"},
				{Op: OpCPU, Ticks: 1},
			}},
			{ID: "M", Base: 5, Arrival: 2, Program: Program{{Op: OpCPU, Ticks: 6}}},
			{ID: "H", Base: 9, Arrival: 3, Program: Program{
				{Op: OpCPU, Ticks: 1},
				{Op: OpLock, Lock: "A"},
				{Op: OpCPU, Ticks: 1},
				{Op: OpUnlock, Lock: "A"},
			}},
		},
	}
	r, _ := New(cfg)
	rep := r.Run()
	if countType(rep, EvPriorityBoost) != 0 {
		t.Errorf("none mode must never boost, got %d", countType(rep, EvPriorityBoost))
	}
	if findTask(rep, "L").Effective != 1 {
		t.Errorf("L eff changed in none mode: %d", findTask(rep, "L").Effective)
	}
	// H must have been blocked behind M: blockedTicks should reflect waiting.
	h := findTask(rep, "H")
	if h.BlockedTicks < 4 {
		t.Errorf("expected H to be blocked >=4 ticks by M, got %d", h.BlockedTicks)
	}
}

// TestTransitiveChain verifies 3-level propagation and rollback on release.
func TestTransitiveChain(t *testing.T) {
	cfg := Config{
		Locks: []LockSpec{{ID: "A"}, {ID: "B"}},
		Tasks: []TaskSpec{
			{ID: "low", Base: 1, Arrival: 0, Program: Program{
				{Op: OpLock, Lock: "B"},
				{Op: OpCPU, Ticks: 6},
				{Op: OpUnlock, Lock: "B"},
			}},
			{ID: "mid", Base: 5, Arrival: 1, Program: Program{
				{Op: OpCPU, Ticks: 1},
				{Op: OpLock, Lock: "A"},
				{Op: OpLock, Lock: "B"},
				{Op: OpCPU, Ticks: 1},
				{Op: OpUnlock, Lock: "B"},
				{Op: OpUnlock, Lock: "A"},
			}},
			{ID: "high", Base: 9, Arrival: 3, Program: Program{
				{Op: OpCPU, Ticks: 1},
				{Op: OpLock, Lock: "A"},
				{Op: OpCPU, Ticks: 1},
				{Op: OpUnlock, Lock: "A"},
			}},
		},
	}
	s, _ := New(cfg)
	r := s.Run()
	if !r.Completed {
		t.Fatalf("not completed: dead=%v fatal=%q", r.Deadlocked, r.Fatal)
	}

	// Sequence of effective priority changes for "low": 1 -> 5 -> 9 -> 1.
	var lowChanges []int
	for _, e := range r.Events {
		if (e.Type == EvPriorityBoost || e.Type == EvPriorityReset) && e.Task == "low" {
			lowChanges = append(lowChanges, e.New)
		}
	}
	want := []int{5, 9, 1}
	if len(lowChanges) < 3 {
		t.Fatalf("low changes = %v, want at least %v", lowChanges, want)
	}
	for i, w := range want {
		if lowChanges[i] != w {
			t.Errorf("low change[%d] = %d, want %d (full %v)", i, lowChanges[i], w, lowChanges)
		}
	}
	// mid must reach 9 (transitively), then return to 5.
	var midChanges []int
	for _, e := range r.Events {
		if (e.Type == EvPriorityBoost || e.Type == EvPriorityReset) && e.Task == "mid" {
			midChanges = append(midChanges, e.New)
		}
	}
	sawMid9, sawMid5 := false, false
	for _, v := range midChanges {
		if v == 9 {
			sawMid9 = true
		}
	}
	if findTask(r, "mid").Effective == 5 {
		sawMid5 = true
	}
	if !sawMid9 {
		t.Errorf("mid never reached 9 transitively: %v", midChanges)
	}
	if !sawMid5 {
		t.Errorf("mid did not return to base 5: %v", midChanges)
	}
}

// TestDeadlockDetected checks the wait-for cycle is found and reported once.
func TestDeadlockDetected(t *testing.T) {
	cfg := Config{
		Locks: []LockSpec{{ID: "A"}, {ID: "B"}},
		Tasks: []TaskSpec{
			{ID: "P1", Base: 3, Arrival: 0, Program: Program{
				{Op: OpCPU, Ticks: 1},
				{Op: OpLock, Lock: "A"},
				{Op: OpCPU, Ticks: 2},
				{Op: OpLock, Lock: "B"},
				{Op: OpUnlock, Lock: "B"},
				{Op: OpUnlock, Lock: "A"},
			}},
			{ID: "P2", Base: 6, Arrival: 2, Program: Program{
				{Op: OpCPU, Ticks: 1},
				{Op: OpLock, Lock: "B"},
				{Op: OpCPU, Ticks: 1},
				{Op: OpLock, Lock: "A"},
				{Op: OpUnlock, Lock: "A"},
				{Op: OpUnlock, Lock: "B"},
			}},
		},
	}
	s, _ := New(cfg)
	r := s.Run()
	if !r.Deadlocked {
		t.Fatal("expected deadlock, run completed")
	}
	dls := []Event{}
	for _, e := range r.Events {
		if e.Type == EvDeadlock {
			dls = append(dls, e)
		}
	}
	if len(dls) != 1 {
		t.Fatalf("want 1 deadlock event (deduplicated), got %d", len(dls))
	}
	cyc := strings.Join(dls[0].Cycle, ",")
	if cyc != "P1,P2,P1" && cyc != "P2,P1,P2" {
		t.Errorf("unexpected cycle %v", dls[0].Cycle)
	}
	if findTask(r, "P1").State != StateBlocked || findTask(r, "P2").State != StateBlocked {
		t.Error("both tasks should remain blocked")
	}
}

// TestSelfDeadlock: re-locking an already-held non-recursive lock is fatal.
func TestSelfDeadlock(t *testing.T) {
	cfg := Config{
		Locks: []LockSpec{{ID: "A"}},
		Tasks: []TaskSpec{
			{ID: "T", Base: 5, Arrival: 0, Program: Program{
				{Op: OpLock, Lock: "A"},
				{Op: OpLock, Lock: "A"},
			}},
		},
	}
	s, _ := New(cfg)
	r := s.Run()
	if r.Fatal == "" {
		t.Fatal("expected fatal self-deadlock")
	}
	if !strings.Contains(r.Fatal, "re-acquire") {
		t.Errorf("unexpected fatal reason: %q", r.Fatal)
	}
}

// TestUnlockNotHeld is rejected as an invalid program.
func TestUnlockNotHeld(t *testing.T) {
	cfg := Config{
		Locks: []LockSpec{{ID: "A"}},
		Tasks: []TaskSpec{
			{ID: "T", Base: 5, Arrival: 0, Program: Program{
				{Op: OpUnlock, Lock: "A"},
			}},
		},
	}
	s, _ := New(cfg)
	r := s.Run()
	if r.Fatal == "" {
		t.Fatal("expected fatal for unlock of unheld lock")
	}
}

// TestExitAutoReleaseLIFO: a task that finishes while holding nested locks
// releases them LIFO and wakes waiters; locks end unheld.
func TestExitAutoReleaseLIFO(t *testing.T) {
	cfg := Config{
		QueuePolicy: QueueFIFO,
		Locks:       []LockSpec{{ID: "A"}, {ID: "B"}},
		Tasks: []TaskSpec{
			{ID: "holder", Base: 8, Arrival: 0, Program: Program{
				{Op: OpLock, Lock: "A"},
				{Op: OpLock, Lock: "B"},
				{Op: OpCPU, Ticks: 1},
				// finishes without explicit unlock
			}},
			{ID: "wb", Base: 2, Arrival: 1, Program: Program{
				{Op: OpLock, Lock: "B"},
				{Op: OpCPU, Ticks: 1},
				{Op: OpUnlock, Lock: "B"},
			}},
			{ID: "wa", Base: 1, Arrival: 1, Program: Program{
				{Op: OpLock, Lock: "A"},
				{Op: OpCPU, Ticks: 1},
				{Op: OpUnlock, Lock: "A"},
			}},
		},
	}
	s, _ := New(cfg)
	r := s.Run()
	if !r.Completed {
		t.Fatalf("expected completion: dead=%v fatal=%q", r.Deadlocked, r.Fatal)
	}
	// Auto-release order must be B then A.
	var auto []string
	for _, e := range r.Events {
		if e.Type == EvLockReleased && e.Reason == "auto-release on exit" {
			auto = append(auto, e.Lock)
		}
	}
	if len(auto) != 2 || auto[0] != "B" || auto[1] != "A" {
		t.Errorf("auto release order = %v, want [B A]", auto)
	}
	for _, l := range r.Locks {
		if l.Holder != "" {
			t.Errorf("lock %s still held by %s", l.ID, l.Holder)
		}
	}
}

// TestFIFOvsPriorityQueue verifies waiter selection policies.
func TestFIFOvsPriorityQueue(t *testing.T) {
	build := func(qp QueuePolicy) *Report {
		cfg := Config{
			QueuePolicy: qp,
			Locks:       []LockSpec{{ID: "A"}},
			Tasks: []TaskSpec{
				{ID: "owner", Base: 1, Arrival: 0, Program: Program{
					{Op: OpLock, Lock: "A"},
					{Op: OpCPU, Ticks: 4},
					{Op: OpUnlock, Lock: "A"},
					{Op: OpCPU, Ticks: 4},
				}},
				{ID: "w1", Base: 2, Arrival: 1, Program: Program{
					{Op: OpLock, Lock: "A"}, {Op: OpCPU, Ticks: 1}, {Op: OpUnlock, Lock: "A"},
				}},
				{ID: "w2", Base: 7, Arrival: 2, Program: Program{
					{Op: OpLock, Lock: "A"}, {Op: OpCPU, Ticks: 1}, {Op: OpUnlock, Lock: "A"},
				}},
			},
		}
		s, _ := New(cfg)
		return s.Run()
	}

	firstWake := func(r *Report) string {
		for _, e := range r.Events {
			if e.Type == EvWakeup && e.Lock == "A" {
				return e.Task
			}
		}
		return ""
	}

	if got := firstWake(build(QueueFIFO)); got != "w1" {
		t.Errorf("FIFO first woken = %q, want w1", got)
	}
	if got := firstWake(build(QueuePriority)); got != "w2" {
		t.Errorf("priority queue first woken = %q, want w2", got)
	}
}

// TestMultipleDonors: owner inherits the maximum of several waiters and drops
// to the next-highest donor when one leaves.
func TestMultipleDonors(t *testing.T) {
	cfg := Config{
		QueuePolicy: QueueFIFO,
		Locks:       []LockSpec{{ID: "A"}},
		Tasks: []TaskSpec{
			{ID: "L", Base: 1, Arrival: 0, Program: Program{
				{Op: OpLock, Lock: "A"},
				{Op: OpCPU, Ticks: 8},
				{Op: OpUnlock, Lock: "A"},
			}},
			{ID: "M", Base: 4, Arrival: 1, Program: Program{
				{Op: OpCPU, Ticks: 1},
				{Op: OpLock, Lock: "A"},
				{Op: OpCPU, Ticks: 1},
				{Op: OpUnlock, Lock: "A"},
			}},
			{ID: "H", Base: 9, Arrival: 2, Program: Program{
				{Op: OpCPU, Ticks: 1},
				{Op: OpLock, Lock: "A"},
				{Op: OpCPU, Ticks: 1},
				{Op: OpUnlock, Lock: "A"},
			}},
		},
	}
	s, _ := New(cfg)
	r := s.Run()
	if !r.Completed {
		t.Fatalf("not completed: %v %q", r.Deadlocked, r.Fatal)
	}
	l := findTask(r, "L")
	if l.Effective != 1 {
		t.Errorf("L final eff = %d, want 1", l.Effective)
	}
	// L should have been boosted to 9 (max donor), not merely 4.
	boostedTo9 := false
	for _, e := range r.Events {
		if e.Type == EvPriorityBoost && e.Task == "L" && e.New == 9 {
			boostedTo9 = true
		}
	}
	if !boostedTo9 {
		t.Error("L never inherited max donor priority 9")
	}
}

// TestIdleAdvance: processor jumps to next arrival and records an idle event.
func TestIdleAdvance(t *testing.T) {
	cfg := Config{
		Locks: []LockSpec{{ID: "A"}},
		Tasks: []TaskSpec{
			{ID: "T", Base: 5, Arrival: 5, Program: Program{{Op: OpCPU, Ticks: 1}}},
		},
	}
	s, _ := New(cfg)
	r := s.Run()
	if !r.Completed {
		t.Fatal("should complete")
	}
	if countType(r, EvIdle) != 1 {
		t.Errorf("want 1 idle event, got %d", countType(r, EvIdle))
	}
	var idle Event
	for _, e := range r.Events {
		if e.Type == EvIdle {
			idle = e
		}
	}
	if idle.Delta != 5 {
		t.Errorf("idle delta = %d, want 5", idle.Delta)
	}
	if findTask(r, "T").FinishTick != 6 {
		t.Errorf("T finish = %d, want 6", findTask(r, "T").FinishTick)
	}
}

// TestMaxTicks: an unblocked but never-ending configuration is bounded.
func TestMaxTicks(t *testing.T) {
	cfg := Config{
		MaxTicks: 3,
		Locks:    []LockSpec{{ID: "A"}},
		Tasks: []TaskSpec{
			{ID: "T", Base: 5, Arrival: 0, Program: Program{{Op: OpCPU, Ticks: 100}}},
		},
	}
	s, _ := New(cfg)
	r := s.Run()
	if r.Fatal == "" {
		t.Fatal("expected maxTicks fatal")
	}
}

// TestEventSink: every emitted event is also delivered to the streaming sink.
func TestEventSink(t *testing.T) {
	cfg := Config{
		Locks: []LockSpec{{ID: "A"}},
		Tasks: []TaskSpec{
			{ID: "T", Base: 5, Arrival: 0, Program: Program{
				{Op: OpCPU, Ticks: 1},
				{Op: OpLock, Lock: "A"},
				{Op: OpUnlock, Lock: "A"},
			}},
		},
	}
	var got []Event
	s, _ := New(cfg, WithEventSink(func(e Event) { got = append(got, e) }))
	r := s.Run()
	if len(got) != len(r.Events) {
		t.Fatalf("sink events %d != report events %d", len(got), len(r.Events))
	}
	for i := range got {
		if got[i].Seq != r.Events[i].Seq {
			t.Errorf("event order mismatch at %d", i)
		}
	}
}

// TestValidationErrors exercises constructor validation.
func TestValidationErrors(t *testing.T) {
	base := func() Config {
		return Config{
			Locks: []LockSpec{{ID: "A"}},
			Tasks: []TaskSpec{{ID: "T", Base: 1, Arrival: 0,
				Program: Program{{Op: OpCPU, Ticks: 1}}}},
		}
	}
	cases := map[string]func() Config{
		"no tasks": func() Config {
			c := base()
			c.Tasks = nil
			return c
		},
		"no locks": func() Config {
			c := base()
			c.Locks = nil
			return c
		},
		"dup task": func() Config {
			c := base()
			c.Tasks = append(c.Tasks, c.Tasks[0])
			return c
		},
		"dup lock": func() Config {
			c := base()
			c.Locks = append(c.Locks, LockSpec{ID: "A"})
			return c
		},
		"unknown lock": func() Config {
			c := base()
			c.Tasks[0].Program = Program{{Op: OpLock, Lock: "ZZ"}}
			return c
		},
		"zero cpu": func() Config {
			c := base()
			c.Tasks[0].Program = Program{{Op: OpCPU, Ticks: 0}}
			return c
		},
		"bad mode": func() Config {
			c := base()
			c.Inheritance = "weird"
			return c
		},
		"bad queue": func() Config {
			c := base()
			c.QueuePolicy = "weird"
			return c
		},
	}
	for name, build := range cases {
		t.Run(name, func(t *testing.T) {
			if _, err := New(build()); err == nil {
				t.Fatalf("expected error for %s", name)
			}
		})
	}
}

// TestPluggableClock confirms the scheduler drives an injected clock.
func TestPluggableClock(t *testing.T) {
	cc := NewCountingClock(NewSimClock())
	cfg := Config{
		Locks: []LockSpec{{ID: "A"}},
		Tasks: []TaskSpec{
			{ID: "T", Base: 5, Arrival: 0, Program: Program{{Op: OpCPU, Ticks: 3}}},
		},
	}
	s, err := New(cfg, WithClock(cc))
	if err != nil {
		t.Fatal(err)
	}
	r := s.Run()
	if !r.Completed {
		t.Fatal("should complete")
	}
	if cc.Steps != 3 {
		t.Errorf("clock advanced %d, want 3", cc.Steps)
	}
}
