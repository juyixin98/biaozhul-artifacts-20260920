package scheduler

import (
	"errors"
	"strconv"
	"testing"
)

func mustConfigure(t *testing.T, s *Scheduler, cpu, mem int64) {
	t.Helper()
	if err := s.Configure(Resources{cpu, mem}); err != nil {
		t.Fatalf("Configure: %v", err)
	}
}

func mustTenant(t *testing.T, s *Scheduler, name string, weight int64, quota Resources) {
	t.Helper()
	if err := s.AddTenant(name, weight, quota); err != nil {
		t.Fatalf("AddTenant(%s): %v", name, err)
	}
}

// mustSubmit submits a task; wantRunning asserts its state right after the
// scheduling pass.
func mustSubmit(t *testing.T, s *Scheduler, id, tenant string, cpu, mem int64, wantRunning bool) {
	t.Helper()
	task, err := s.Submit(id, tenant, Resources{cpu, mem})
	if err != nil {
		t.Fatalf("Submit(%s): %v", id, err)
	}
	gotRunning := !task.Released && s.active[task.ID] != nil
	if gotRunning != wantRunning {
		t.Fatalf("task %s running=%v, want %v (used=%+v cap=%+v)",
			id, gotRunning, wantRunning, s.used, s.capacity)
	}
}

func runningIDs(s *Scheduler) map[string]bool {
	m := map[string]bool{}
	for id := range s.active {
		m[id] = true
	}
	return m
}

func queuedIDs(s *Scheduler) map[string]bool {
	m := map[string]bool{}
	for _, tn := range s.tenants {
		for _, task := range tn.queue {
			m[task.ID] = true
		}
	}
	return m
}

// TestAcceptanceScenario is the headline acceptance case:
// capacity 10 CPU / 10 GiB; tenant A is CPU-heavy (4c/0m tasks), tenant B is
// memory-heavy (0c/4GiB tasks), equal weights. Six tasks submitted, but
// indivisibility means only four can fit; the other two stay queued. Freeing
// the first two tasks lets everything reschedule.
func TestAcceptanceScenario(t *testing.T) {
	s := New()
	mustConfigure(t, s, 10, 10)
	mustTenant(t, s, "A", 1, Resources{})
	mustTenant(t, s, "B", 1, Resources{})

	// Placement order under DRF: A and B dominant shares stay equal, so the
	// lexicographic tie-break interleaves them: a1,b1,a2,b2.
	mustSubmit(t, s, "a1", "A", 4, 0, true)
	mustSubmit(t, s, "b1", "B", 0, 4, true)
	mustSubmit(t, s, "a2", "A", 4, 0, true)
	mustSubmit(t, s, "b2", "B", 0, 4, true)
	// Cluster is now 8/8 on each dimension; a 4-unit task cannot fit.
	mustSubmit(t, s, "a3", "A", 4, 0, false)
	mustSubmit(t, s, "b3", "B", 0, 4, false)

	snap := s.Snapshot()
	if snap.Used != (Resources{8, 8}) {
		t.Fatalf("used = %+v, want {8 8}", snap.Used)
	}
	if len(snap.Running) != 4 || len(snap.Queued) != 2 {
		t.Fatalf("running=%d queued=%d, want 4 and 2", len(snap.Running), len(snap.Queued))
	}
	// Deterministic placement order recorded via submission sequences.
	wantOrder := []string{"a1", "b1", "a2", "b2"}
	for i, id := range wantOrder {
		if snap.Running[i].ID != id {
			t.Fatalf("running[%d] = %s, want %s", i, snap.Running[i].ID, id)
		}
	}
	// Per-tenant FIFO: a3 sits before any later A task.
	if got := s.tenants["A"].queue[0].ID; got != "a3" {
		t.Fatalf("A head of queue = %s, want a3", got)
	}

	// Release a1 (4 CPU): a3 can now run; b3 still waits on memory.
	if err := s.Release("a1"); err != nil {
		t.Fatal(err)
	}
	if !runningIDs(s)["a3"] || !queuedIDs(s)["b3"] {
		t.Fatalf("after release a1: a3 should run, b3 queued; running=%v queued=%v",
			runningIDs(s), queuedIDs(s))
	}
	// Release b1 (4 MiB): b3 reschedules; all six tasks running.
	if err := s.Release("b1"); err != nil {
		t.Fatal(err)
	}
	if len(s.active) != 4 {
		t.Fatalf("active count = %d, want 4 (a2,a3,b2,b3)", len(s.active))
	}
	for _, id := range []string{"a2", "a3", "b2", "b3"} {
		if !runningIDs(s)[id] {
			t.Fatalf("expected %s running after reschedule", id)
		}
	}
	if s.used != (Resources{8, 8}) {
		t.Fatalf("used after reschedule = %+v, want {8 8}", s.used)
	}
}

// TestConservationInvariant fuzzes mixed demands and checks that the
// scheduler never over-allocates and that the bookkeeping total equals the
// sum of running task demands. It also verifies every queued task genuinely
// cannot fit the remaining capacity (the pass is a fixed point).
func TestConservationInvariant(t *testing.T) {
	s := New()
	mustConfigure(t, s, 10, 10)
	mustTenant(t, s, "x", 1, Resources{})
	mustTenant(t, s, "y", 2, Resources{})
	mustTenant(t, s, "z", 3, Resources{})

	// Deterministic LCG (mask keeps seed non-negative) so the test is
	// reproducible.
	var seed int64 = 42
	next := func() int64 {
		seed = (seed*1103515245 + 12345) & 0x7fffffff
		return (seed>>16)%4 + 1 // 1..4
	}
	tenants := []string{"x", "y", "z"}
	submitted := map[string]Resources{}
	for i := 0; i < 60; i++ {
		name := tenants[i%3]
		id := name + "-" + strconv.Itoa(i)
		d := Resources{next(), next()}
		_, err := s.Submit(id, name, d)
		if err != nil {
			t.Fatalf("submit %s: %v", id, err)
		}
		submitted[id] = d
	}

	snap := s.Snapshot()
	if !snap.Used.within(snap.Capacity) {
		t.Fatalf("used %+v exceeds capacity %+v", snap.Used, snap.Capacity)
	}
	// Sum of running demands equals scheduler-level used.
	var sum Resources
	running := map[string]bool{}
	for _, tv := range snap.Running {
		sum = sum.add(tv.Demand)
		running[tv.ID] = true
	}
	if sum != snap.Used {
		t.Fatalf("sum running demands %+v != used %+v", sum, snap.Used)
	}
	// Per-tenant usage also sums to the total.
	var tsum Resources
	for _, tv := range snap.Tenants {
		tsum = tsum.add(tv.Used)
	}
	if tsum != snap.Used {
		t.Fatalf("sum tenant used %+v != used %+v", tsum, snap.Used)
	}
	// Scheduling pass fixed point: no tenant head fits the free resources
	// (FIFO means a tail may fit behind a blocked head; the head is what the
	// pass can legally place next).
	for _, tn := range s.tenants {
		if len(tn.queue) == 0 {
			continue
		}
		head := tn.queue[0].Demand
		if head.within(snap.Free) {
			t.Fatalf("tenant %s head demand %+v fits free %+v; scheduler not at fixed point",
				tn.Name, head, snap.Free)
		}
	}
	// Another scheduling pass must not change anything.
	before := len(snap.Running)
	s.scheduleLocked()
	if len(s.active) != before {
		t.Fatalf("rerunning schedule changed running count %d -> %d", before, len(s.active))
	}
}

// TestDeterministicTieBreak builds a state where two tenants have exactly
// equal weighted dominant share and both have fitting queued tasks for only
// one slot; the lexicographically smaller tenant id must win. Repeated
// replays give identical results.
//
// Single-dimension setup (all tasks need 4 CPU on a 12-CPU cluster):
// after x1,z1,x2 are placed both tenants sit at 4/12; releasing x1 leaves
// one slot that both head tasks compete for with equal shares.
func TestDeterministicTieBreak(t *testing.T) {
	for replay := 0; replay < 5; replay++ {
		s := New()
		mustConfigure(t, s, 12, 12)
		mustTenant(t, s, "x", 1, Resources{})
		mustTenant(t, s, "z", 1, Resources{})

		mustSubmit(t, s, "x1", "x", 4, 0, true)
		mustSubmit(t, s, "z1", "z", 4, 0, true)
		mustSubmit(t, s, "x2", "x", 4, 0, true) // tie at 4/12 each -> x wins by name
		mustSubmit(t, s, "x3", "x", 4, 0, false)
		mustSubmit(t, s, "z2", "z", 4, 0, false)

		// Release x1: x still holds x2 (4 CPU), z holds z1 (4 CPU): equal
		// dominant shares, both heads are 4 CPU, only one slot open.
		if err := s.Release("x1"); err != nil {
			t.Fatal(err)
		}
		if !runningIDs(s)["x3"] {
			t.Fatalf("replay %d: x3 should win the tie", replay)
		}
		if runningIDs(s)["z2"] {
			t.Fatalf("replay %d: z2 must stay queued after x takes the slot", replay)
		}

		// Releasing z1 opens a slot for z2 via rescheduling.
		if err := s.Release("z1"); err != nil {
			t.Fatal(err)
		}
		if !runningIDs(s)["z2"] {
			t.Fatalf("replay %d: z2 should run after z1 is released", replay)
		}
	}
}

// TestLessWeightedShare unit-checks the exact comparison: weight 2 tenant at
// 4/10 has smaller weighted share than weight 1 tenant at 4/10; ties resolve
// by name in both directions.
func TestLessWeightedShare(t *testing.T) {
	cap := Resources{10, 10}
	a := &Tenant{Name: "a", Weight: 2}
	b := &Tenant{Name: "b", Weight: 1}
	a.used = Resources{4, 0}
	b.used = Resources{4, 0}
	if !lessWeightedShare(a, b, cap) {
		t.Fatal("weight-2 tenant at equal absolute use should be scheduled first")
	}
	if lessWeightedShare(b, a, cap) {
		t.Fatal("inverse comparison must be false")
	}
	// Equal weights, equal shares -> name order.
	c := &Tenant{Name: "c", Weight: 1, used: Resources{2, 2}}
	d := &Tenant{Name: "d", Weight: 1, used: Resources{2, 2}}
	if !lessWeightedShare(c, d, cap) || lessWeightedShare(d, c, cap) {
		t.Fatal("tie must resolve lexicographically")
	}
}

// TestWeightsPriority verifies that when two equally-used tenants contend
// for one freed slot, the tenant with weight 2 is chosen over weight 1.
func TestWeightsPriority(t *testing.T) {
	s := New()
	mustConfigure(t, s, 12, 12)
	mustTenant(t, s, "hi", 2, Resources{})
	mustTenant(t, s, "lo", 1, Resources{})

	mustSubmit(t, s, "hi1", "hi", 4, 0, true)
	mustSubmit(t, s, "lo1", "lo", 4, 0, true)
	mustSubmit(t, s, "hi2", "hi", 4, 0, true) // cluster 12/12
	mustSubmit(t, s, "lo2", "lo", 4, 0, false)
	mustSubmit(t, s, "hi3", "hi", 4, 0, false)

	// Release hi1: both tenants have 4 CPU in use and an eligible 4-CPU
	// head. hi weighted share = 4/(12*2), lo = 4/12 -> hi wins.
	if err := s.Release("hi1"); err != nil {
		t.Fatal(err)
	}
	if !runningIDs(s)["hi3"] || !queuedIDs(s)["lo2"] {
		t.Fatalf("weighted priority violated: running=%v queued=%v",
			runningIDs(s), queuedIDs(s))
	}
}

// TestQuota verifies absolute per-tenant quota caps are respected even when
// the cluster has free resources, that over-quota demands are rejected, and
// that releasing does not bypass the cap.
func TestQuota(t *testing.T) {
	s := New()
	mustConfigure(t, s, 10, 10)
	mustTenant(t, s, "q", 1, Resources{CPU: 3}) // 3 CPU cap, memory unlimited
	mustTenant(t, s, "f", 1, Resources{})

	mustSubmit(t, s, "q1", "q", 2, 0, true)
	mustSubmit(t, s, "q2", "q", 2, 0, false) // would put q at 4 > quota 3
	mustSubmit(t, s, "f1", "f", 8, 0, true)  // free tenant fills the cluster

	if s.tenants["q"].used.CPU > 3 {
		t.Fatalf("quota breached: q used %d CPU", s.tenants["q"].used.CPU)
	}
	// Freeing cluster resources does not help q2 past the quota.
	if err := s.Release("f1"); err != nil {
		t.Fatal(err)
	}
	if runningIDs(s)["q2"] {
		t.Fatal("q2 must not run: it exceeds tenant quota")
	}
	// A demand larger than the quota is rejected up front.
	if _, err := s.Submit("q3", "q", Resources{4, 0}); err == nil {
		t.Fatal("demand exceeding quota should be rejected")
	}
	// Memory quota works on its own dimension.
	mustTenant(t, s, "r", 1, Resources{Mem: 5})
	if _, err := s.Submit("r1", "r", Resources{0, 6}); !errors.Is(err, ErrBadRequest) {
		t.Fatalf("over-quota memory demand should be ErrBadRequest, got %v", err)
	}
}

// TestIndivisibilityFragmentation demonstrates the fairness deviation caused
// by discrete tasks: free resources exist but cannot be used by the waiting
// task. Release makes them available again.
func TestIndivisibilityFragmentation(t *testing.T) {
	s := New()
	mustConfigure(t, s, 6, 6)
	mustTenant(t, s, "t", 1, Resources{})

	mustSubmit(t, s, "big", "t", 4, 4, true)
	mustSubmit(t, s, "next", "t", 4, 0, false) // 2/2 free, unusable for a 4-CPU task

	snap := s.Snapshot()
	if snap.Free != (Resources{2, 2}) {
		t.Fatalf("free = %+v, want {2 2}", snap.Free)
	}
	if !queuedIDs(s)["next"] {
		t.Fatal("next should remain queued despite free resources (fragmentation)")
	}
	if err := s.Release("big"); err != nil {
		t.Fatal(err)
	}
	if !runningIDs(s)["next"] {
		t.Fatal("next should run after big is released")
	}
	if s.used != (Resources{4, 0}) {
		t.Fatalf("used = %+v, want {4 0}", s.used)
	}
}

// TestImpossibleAndInvalidTasks covers rejection paths.
func TestImpossibleAndInvalidTasks(t *testing.T) {
	s := New()
	mustConfigure(t, s, 5, 5)
	mustTenant(t, s, "a", 1, Resources{})

	if _, err := s.Submit("big", "a", Resources{6, 1}); !errors.Is(err, ErrBadRequest) {
		t.Fatalf("demand above capacity should be rejected, got %v", err)
	}
	if _, err := s.Submit("dup", "a", Resources{1, 1}); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Submit("dup", "a", Resources{1, 1}); !errors.Is(err, ErrTaskExists) {
		t.Fatalf("duplicate id should be ErrTaskExists, got %v", err)
	}
	if _, err := s.Submit("ghost", "nobody", Resources{1, 1}); !errors.Is(err, ErrTenantNotFound) {
		t.Fatalf("unknown tenant should be ErrTenantNotFound, got %v", err)
	}
	if err := s.Release("big"); !errors.Is(err, ErrTaskNotFound) {
		t.Fatalf("releasing unknown task should be ErrTaskNotFound, got %v", err)
	}
}

// TestReleaseQueuedTaskRejected ensures releasing a queued (never placed)
// task is rejected and does not corrupt usage accounting.
func TestReleaseQueuedTaskRejected(t *testing.T) {
	s := New()
	mustConfigure(t, s, 4, 4)
	mustTenant(t, s, "a", 1, Resources{})
	mustSubmit(t, s, "a1", "a", 4, 4, true)
	mustSubmit(t, s, "a2", "a", 1, 1, false) // queued

	if err := s.Release("a2"); !errors.Is(err, ErrBadRequest) {
		t.Fatalf("releasing queued task should be ErrBadRequest, got %v", err)
	}
	if s.tenants["a"].used != (Resources{4, 4}) {
		t.Fatalf("tenant usage corrupted: %+v", s.tenants["a"].used)
	}
	if s.used != (Resources{4, 4}) {
		t.Fatalf("cluster usage corrupted: %+v", s.used)
	}
	// Double release of a running task is also rejected.
	if err := s.Release("a1"); err != nil {
		t.Fatal(err)
	}
	if err := s.Release("a1"); !errors.Is(err, ErrBadRequest) {
		t.Fatalf("double release should be ErrBadRequest, got %v", err)
	}
}

// TestConfigureGuards checks capacity validation and reconfigure-while-busy.
func TestConfigureGuards(t *testing.T) {
	s := New()
	if err := s.Configure(Resources{0, 1}); !errors.Is(err, ErrInvalidCapacity) {
		t.Fatalf("got %v, want ErrInvalidCapacity", err)
	}
	mustConfigure(t, s, 4, 4)
	mustTenant(t, s, "a", 1, Resources{})
	mustSubmit(t, s, "a1", "a", 1, 1, true)
	if err := s.Configure(Resources{100, 100}); !errors.Is(err, ErrAlreadyConfig) {
		t.Fatalf("reconfigure with active tasks should fail, got %v", err)
	}
	s.Reset()
	if s.Snapshot().Configured {
		t.Fatal("reset should clear configuration")
	}
}
