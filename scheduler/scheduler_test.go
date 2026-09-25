package scheduler

import (
	"math/rand"
	"testing"
	"time"
)

func newTestScheduler(t *testing.T, cap Resources) (*Scheduler, *FakeClock, *MemoryStore) {
	t.Helper()
	clock := NewFakeClock(time.Unix(0, 0).UTC())
	store := NewMemoryStore()
	s, err := New(cap, WithClock(clock), WithExecutor(NewTimedExecutor(clock)), WithEventStore(store))
	if err != nil {
		t.Fatal(err)
	}
	return s, clock, store
}

// TestWeightedShareOrdering verifies weighted DRF (weight X=1, Y=2) under
// contention. Capacity is 10 slots ((1,1) tasks on a 10/10 cluster).
//
// Setup: 9 filler tasks from Z hold 9 slots for 1s; x1 occupies the 10th
// slot at t=0. X/Y long tasks are all queued. At t=1 the nine fillers
// release together; the single scheduling pass then admits nine tasks,
// each launch re-sorting tenants by dominant-share/weight:
//
//	state (X running, Y running), starting at (1,0) because x1 ran at t=0:
//	(1,0)->Y (1,1)->Y (1,2)->X (2,2)->Y (2,3)->Y (2,4)->X
//	(3,4)->Y (3,5)->Y (3,6)->X (4,6)
//
// Steady state X=4, Y=6 (ratio 1:2), and the admission order within the
// t=1 pass is exactly y1,y2,x2,y3,y4,x3,y5,y6,x4.
func TestWeightedShareOrdering(t *testing.T) {
	s, clock, store := newTestScheduler(t, Resources{CPU: 10, Mem: 10})
	for _, w := range []struct {
		id string
		w  int64
	}{{"Z", 1}, {"X", 1}, {"Y", 2}} {
		if err := s.AddTenant(w.id, w.w); err != nil {
			t.Fatal(err)
		}
	}
	for i := 1; i <= 9; i++ {
		if err := s.Submit(TaskSpec{
			ID: "z" + itoa(i), TenantID: "Z",
			Request:  Resources{CPU: 1, Mem: 1},
			Duration: Duration{Duration: time.Second},
		}); err != nil {
			t.Fatal(err)
		}
	}
	submit := func(id, tenant string) {
		t.Helper()
		if err := s.Submit(TaskSpec{
			ID: id, TenantID: tenant,
			Request:  Resources{CPU: 1, Mem: 1},
			Duration: Duration{Duration: time.Hour},
		}); err != nil {
			t.Fatal(err)
		}
	}
	submit("x1", "X") // takes the 10th slot at t=0
	for i := 1; i <= 10; i++ {
		submit("y"+itoa(i), "Y")
	}
	for i := 2; i <= 10; i++ {
		submit("x"+itoa(i), "X")
	}
	if got := statusOf(t, s.Snapshot(), "y1"); got != StatusWaiting {
		t.Fatalf("y1 at t=0 = %s, want WAITING (cluster full)", got)
	}

	clock.Advance(time.Second)
	starts, _ := startFinishOrder(store.All())
	// Starts at t=1 are the tail after the ten t=0 starts.
	got := starts[10:]
	want := []string{"y1", "y2", "x2", "y3", "y4", "x3", "y5", "y6", "x4"}
	if !equalStrings(got, want) {
		t.Fatalf("weighted admission order = %v, want %v", got, want)
	}
	snap := s.Snapshot()
	var xCount, yCount int
	for _, tk := range snap.Tasks {
		if tk.Status == StatusRunning {
			switch tk.TenantID {
			case "X":
				xCount++
			case "Y":
				yCount++
			}
		}
	}
	if xCount != 4 || yCount != 6 {
		t.Fatalf("weighted steady state X=%d Y=%d, want 4 and 6", xCount, yCount)
	}
}

// TestDeterministicTieBreak: when equal-weight tenants reach the same
// scheduling decision with equal weighted shares and equal running
// counts, lexicographic tenant id breaks the tie — regardless of the
// order tenants/tasks were registered in.
func TestDeterministicTieBreak(t *testing.T) {
	s, clock, store := newTestScheduler(t, Resources{CPU: 10, Mem: 10})
	// Fillers pin all 10 slots until t=1, leaving exactly one slot contested
	// simultaneously by charlie/bob/alice at release time.
	if err := s.AddTenant("filler", 1); err != nil {
		t.Fatal(err)
	}
	for i := 1; i <= 10; i++ {
		if err := s.Submit(TaskSpec{
			ID: "f" + itoa(i), TenantID: "filler",
			Request:  Resources{CPU: 1, Mem: 1},
			Duration: Duration{Duration: time.Second},
		}); err != nil {
			t.Fatal(err)
		}
	}
	// Register tenants, then submit tasks, both in reverse alphabetical.
	for _, id := range []string{"charlie", "bob", "alice"} {
		if err := s.AddTenant(id, 1); err != nil {
			t.Fatal(err)
		}
	}
	for _, id := range []string{"charlie", "bob", "alice"} {
		if err := s.Submit(TaskSpec{
			ID: "t-" + id, TenantID: id,
			Request:  Resources{CPU: 1, Mem: 1},
			Duration: Duration{Duration: time.Hour},
		}); err != nil {
			t.Fatal(err)
		}
	}
	for _, tk := range s.Snapshot().Tasks {
		if tk.TenantID != "filler" && tk.Status != StatusWaiting {
			t.Fatalf("%s must be WAITING before fillers release", tk.ID)
		}
	}
	clock.Advance(time.Second)
	starts, _ := startFinishOrder(store.All())
	// First three starts after the ten fillers are the tie resolution;
	// order must be alice, bob, charlie (lexicographic).
	got := starts[10:13]
	want := []string{"t-alice", "t-bob", "t-charlie"}
	if !equalStrings(got, want) {
		t.Fatalf("tie-break order = %v, want %v", got, want)
	}

	// Re-run the identical workload in a fresh scheduler to prove
	// determinism (no map-iteration dependence).
	clock2 := NewFakeClock(time.Unix(0, 0).UTC())
	s2, err := New(Resources{CPU: 10, Mem: 10}, WithClock(clock2), WithExecutor(NewTimedExecutor(clock2)))
	if err != nil {
		t.Fatal(err)
	}
	for _, id := range []string{"charlie", "filler", "alice", "bob"} {
		if err := s2.AddTenant(id, 1); err != nil {
			t.Fatal(err)
		}
	}
	for i := 1; i <= 10; i++ {
		if err := s2.Submit(TaskSpec{ID: "f" + itoa(i), TenantID: "filler",
			Request: Resources{CPU: 1, Mem: 1}, Duration: Duration{time.Second}}); err != nil {
			t.Fatal(err)
		}
	}
	for _, id := range []string{"bob", "charlie", "alice"} {
		if err := s2.Submit(TaskSpec{ID: "t-" + id, TenantID: id,
			Request: Resources{CPU: 1, Mem: 1}, Duration: Duration{time.Hour}}); err != nil {
			t.Fatal(err)
		}
	}
	clock2.Advance(time.Second)
	starts2, _ := startFinishOrder(s2.Store().All())
	got2 := starts2[10:13]
	if !equalStrings(got2, want) {
		t.Fatalf("non-deterministic: second run gave %v, want %v", got2, want)
	}
}

// TestPermanentHeadBlockIsTenantLocal: a task exceeding total capacity
// permanently blocks its own tenant FIFO but never affects other tenants.
func TestPermanentHeadBlockIsTenantLocal(t *testing.T) {
	s, _, store := newTestScheduler(t, Resources{CPU: 5, Mem: 5})
	if err := s.AddTenant("A", 1); err != nil {
		t.Fatal(err)
	}
	if err := s.AddTenant("B", 1); err != nil {
		t.Fatal(err)
	}
	if err := s.Submit(TaskSpec{
		ID: "huge", TenantID: "A",
		Request:  Resources{CPU: 9, Mem: 9},
		Duration: Duration{Duration: time.Hour},
	}); err != nil {
		t.Fatal(err)
	}
	if err := s.Submit(TaskSpec{
		ID: "b1", TenantID: "B",
		Request:  Resources{CPU: 1, Mem: 1},
		Duration: Duration{Duration: time.Hour},
	}); err != nil {
		t.Fatal(err)
	}
	snap := s.Snapshot()
	if statusOf(t, snap, "b1") != StatusRunning {
		t.Fatal("B must run while A head is infeasible")
	}
	a := tenantView(snap, "A")
	if !a.HeadBlocked || !a.HeadExceedsCap {
		t.Fatal("A head must be flagged permanently blocked")
	}
	var blocked int
	for _, ev := range store.All() {
		if ev.Type == EventTaskBlocked {
			blocked++
			if ev.Reason == "" {
				t.Fatal("blocked event must carry a reason")
			}
		}
	}
	if blocked != 1 {
		t.Fatalf("blocked events = %d, want 1", blocked)
	}

	// A second, feasible A task still waits behind the infeasible head.
	if err := s.Submit(TaskSpec{
		ID: "a-small", TenantID: "A",
		Request:  Resources{CPU: 1, Mem: 1},
		Duration: Duration{Duration: time.Hour},
	}); err != nil {
		t.Fatal(err)
	}
	if got := statusOf(t, s.Snapshot(), "a-small"); got != StatusWaiting {
		t.Fatalf("a-small = %s, must wait behind infeasible FIFO head", got)
	}
	if got := statusOf(t, s.Snapshot(), "b1"); got != StatusRunning {
		t.Fatalf("b1 = %s, other tenant must be unaffected", got)
	}
}

// TestEventsAreStructured verifies event content at submit/start/finish.
func TestEventsAreStructured(t *testing.T) {
	s, clock, store := newTestScheduler(t, Resources{CPU: 4, Mem: 4})
	if err := s.AddTenant("A", 3); err != nil {
		t.Fatal(err)
	}
	if err := s.Submit(TaskSpec{
		ID: "job", TenantID: "A",
		Request:  Resources{CPU: 2, Mem: 3},
		Duration: Duration{Duration: 2 * time.Second},
	}); err != nil {
		t.Fatal(err)
	}
	evs := store.All()
	if len(evs) != 3 { // tenant created, submitted, started
		t.Fatalf("events after submit = %d, want 3", len(evs))
	}
	last := evs[2]
	if last.Type != EventTaskStarted {
		t.Fatalf("event type = %s", last.Type)
	}
	if last.Request == nil || *last.Request != (Resources{CPU: 2, Mem: 3}) {
		t.Fatalf("started event request = %+v", last.Request)
	}
	if last.Used == nil || *last.Used != (Resources{CPU: 2, Mem: 3}) {
		t.Fatalf("started event used = %+v", last.Used)
	}
	if last.Available == nil || *last.Available != (Resources{CPU: 2, Mem: 1}) {
		t.Fatalf("started event available = %+v", last.Available)
	}
	if last.EventID != 3 || !last.At.Equal(clock.Now()) {
		t.Fatalf("event id/at wrong: %+v", last)
	}

	clock.Advance(2 * time.Second)
	if got := statusOf(t, s.Snapshot(), "job"); got != StatusDone {
		t.Fatalf("after duration job = %s", got)
	}
	fin := store.All()[3]
	if fin.Type != EventTaskFinished || fin.Used == nil || *fin.Used != (Resources{CPU: 0, Mem: 0}) {
		t.Fatalf("finish event wrong: %+v", fin)
	}
	// Since(afterID) pagination.
	if got := store.Since(2, 0); len(got) != 2 || got[0].EventID != 3 {
		t.Fatalf("Since pagination wrong: %+v", got)
	}
}

// TestSmallTaskFitsBehindBigHead: within one scheduling pass, the
// highest-priority tenant's head may not fit the current availability,
// but a later tenant's smaller head still must start. The pass must skip
// (not stop at) the over-sized head.
func TestSmallTaskFitsBehindBigHead(t *testing.T) {
	s, _, _ := newTestScheduler(t, Resources{CPU: 10, Mem: 10})
	if err := s.AddTenant("A", 1); err != nil {
		t.Fatal(err)
	}
	if err := s.AddTenant("B", 1); err != nil {
		t.Fatal(err)
	}
	must := func(err error) {
		t.Helper()
		if err != nil {
			t.Fatal(err)
		}
	}
	// A starts a task consuming 9 CPU / 9 Mem (1 CPU, 1 Mem left).
	must(s.Submit(TaskSpec{ID: "a-run", TenantID: "A", Request: Resources{CPU: 9, Mem: 9},
		Duration: Duration{Duration: time.Hour}}))
	// A then queues a head needing 3 CPU / 3 Mem (does not fit 1/1 avail).
	must(s.Submit(TaskSpec{ID: "a-big", TenantID: "A", Request: Resources{CPU: 3, Mem: 3},
		Duration: Duration{Duration: time.Hour}}))
	// B's small task fits the remaining 1/1.
	must(s.Submit(TaskSpec{ID: "b-small", TenantID: "B", Request: Resources{CPU: 1, Mem: 1},
		Duration: Duration{Duration: time.Hour}}))
	snap := s.Snapshot()
	if statusOf(t, snap, "a-big") != StatusWaiting {
		t.Fatal("a-big must WAIT (does not fit)")
	}
	if statusOf(t, snap, "b-small") != StatusRunning {
		t.Fatal("b-small must RUN: lower-priority head still fits available resources")
	}
	if snap.Used != (Resources{CPU: 10, Mem: 10}) {
		t.Fatalf("used = %v, want {10 10}", snap.Used)
	}
}

// TestNonPreemptibleAndReuse: once started, a running task is never
// evicted; after it finishes its resources go to the next waiter.
func TestNonPreemptibleAndReuse(t *testing.T) {
	s, clock, _ := newTestScheduler(t, Resources{CPU: 3, Mem: 3})
	for _, id := range []string{"A", "B"} {
		if err := s.AddTenant(id, 1); err != nil {
			t.Fatal(err)
		}
	}
	must := func(err error) {
		t.Helper()
		if err != nil {
			t.Fatal(err)
		}
	}
	must(s.Submit(TaskSpec{ID: "a1", TenantID: "A", Request: Resources{CPU: 3, Mem: 1},
		Duration: Duration{Duration: time.Second}}))
	must(s.Submit(TaskSpec{ID: "b1", TenantID: "B", Request: Resources{CPU: 1, Mem: 3},
		Duration: Duration{Duration: time.Hour}}))
	if statusOf(t, s.Snapshot(), "b1") != StatusWaiting {
		t.Fatal("b1 cannot fit while a1 holds 3 CPU")
	}
	clock.Advance(time.Second)
	if statusOf(t, s.Snapshot(), "b1") != StatusRunning {
		t.Fatal("b1 must start once a1 frees CPU")
	}
	if s.Snapshot().Used != (Resources{CPU: 1, Mem: 3}) {
		t.Fatalf("used after reuse = %v", s.Snapshot().Used)
	}
}

// TestNoOvercommitRandomized drives a long pseudo-random workload against
// the fake clock and asserts after every step that:
//   - used never exceeds capacity in either dimension;
//   - per-tenant allocated resources equal their running task requests;
//   - allocations sum to cluster used.
func TestNoOvercommitRandomized(t *testing.T) {
	const capCPU, capMem = 10, 12
	s, clock, _ := newTestScheduler(t, Resources{CPU: capCPU, Mem: capMem})
	rng := rand.New(rand.NewSource(424242))
	for i := 0; i < 4; i++ {
		if err := s.AddTenant(string(rune('A'+i)), int64(1+rng.Intn(3))); err != nil {
			t.Fatal(err)
		}
	}

	assertInvariants := func(stage string) {
		t.Helper()
		snap := s.Snapshot()
		if snap.Used.CPU > capCPU || snap.Used.Mem > capMem {
			t.Fatalf("[%s] OVERCOMMIT used=%v cap={%d %d}", stage, snap.Used, capCPU, capMem)
		}
		var sumCPU, sumMem int64
		for _, tv := range snap.Tenants {
			var rCPU, rMem int64
			for _, tk := range tv.Running {
				rCPU += tk.Request.CPU
				rMem += tk.Request.Mem
			}
			if rCPU != tv.Allocated.CPU || rMem != tv.Allocated.Mem {
				t.Fatalf("[%s] tenant %s allocated=%v but running sums {%d %d}",
					stage, tv.ID, tv.Allocated, rCPU, rMem)
			}
			sumCPU += rCPU
			sumMem += rMem
		}
		if sumCPU != snap.Used.CPU || sumMem != snap.Used.Mem {
			t.Fatalf("[%s] tenant allocations {%d %d} != used %v", stage, sumCPU, sumMem, snap.Used)
		}
	}

	for round := 0; round < 400; round++ {
		tenant := string(rune('A' + rng.Intn(4)))
		// Mix of small, large-but-feasible, and occasionally infeasible tasks.
		cpu := int64(1 + rng.Intn(capCPU+2))
		mem := int64(1 + rng.Intn(capMem+2))
		d := time.Duration(1+rng.Intn(5)) * time.Second
		id := "task-" + itoa(round)
		err := s.Submit(TaskSpec{
			ID: id, TenantID: tenant,
			Request:  Resources{CPU: cpu, Mem: mem},
			Duration: Duration{Duration: d},
		})
		if err != nil {
			t.Fatalf("round %d submit: %v", round, err)
		}
		assertInvariants("submit/" + id)
		if rng.Intn(2) == 0 {
			clock.Advance(time.Duration(1+rng.Intn(3)) * time.Second)
			assertInvariants("tick/" + id)
		}
	}
	// Drain everything; all feasible tasks must eventually finish.
	clock.Advance(30 * time.Second)
	assertInvariants("drained")
	snap := s.Snapshot()
	if snap.Used != (Resources{CPU: 0, Mem: 0}) {
		t.Fatalf("cluster not drained: used=%v", snap.Used)
	}
	// The only waiting tasks left are tenant queues whose head is
	// infeasible (> capacity); per-tenant FIFO then parks the tail too,
	// which is the documented head-of-line behavior. Every waiting queue
	// MUST be parked behind an infeasible head, and a feasible task may
	// only wait if some preceding task in its tenant queue is infeasible.
	for _, tv := range snap.Tenants {
		if len(tv.Waiting) == 0 {
			continue
		}
		head := tv.Waiting[0]
		if head.Request.CPU <= capCPU && head.Request.Mem <= capMem {
			t.Fatalf("tenant %s has feasible head %s %v still waiting after drain",
				tv.ID, head.ID, head.Request)
		}
	}
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var b [20]byte
	i := len(b)
	for n > 0 {
		i--
		b[i] = byte('0' + n%10)
		n /= 10
	}
	return string(b[i:])
}

// TestValidationErrors covers request validation paths.
func TestValidationErrors(t *testing.T) {
	if _, err := New(Resources{CPU: 0, Mem: 1}); err == nil {
		t.Fatal("zero CPU capacity must be rejected")
	}
	s, _, _ := newTestScheduler(t, Resources{CPU: 1, Mem: 1})
	if err := s.AddTenant("A", 0); err == nil {
		t.Fatal("weight 0 must be rejected")
	}
	if err := s.AddTenant("A", 1); err != nil {
		t.Fatal(err)
	}
	if err := s.AddTenant("A", 1); err == nil {
		t.Fatal("duplicate tenant must be rejected")
	}
	if err := s.Submit(TaskSpec{ID: "x", TenantID: "ghost", Request: Resources{CPU: 1, Mem: 1},
		Duration: Duration{Duration: time.Second}}); err == nil {
		t.Fatal("unknown tenant must be rejected")
	}
	bad := func(spec TaskSpec) {
		t.Helper()
		if err := s.Submit(spec); err == nil {
			t.Fatalf("bad spec accepted: %+v", spec)
		}
	}
	good := Resources{CPU: 1, Mem: 1}
	bad(TaskSpec{ID: "", TenantID: "A", Request: good, Duration: Duration{time.Second}})
	bad(TaskSpec{ID: "y", TenantID: "A", Request: Resources{CPU: 0, Mem: 1}, Duration: Duration{time.Second}})
	bad(TaskSpec{ID: "z", TenantID: "A", Request: good, Duration: Duration{0}})
	if err := s.Complete("missing"); err == nil {
		t.Fatal("completing unknown task must fail")
	}
}
