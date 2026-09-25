package scheduler

import (
	"errors"
	"testing"
	"time"
)

func errorIs(err, target error) bool { return errors.Is(err, target) }

// testEnv bundles a fake-clock scheduler with an in-memory event sink.
type testEnv struct {
	s    *Scheduler
	clk  *FakeClock
	sink *MemorySink
}

func newTestEnv(t *testing.T, cap Resources) *testEnv {
	t.Helper()
	clk := NewFakeClock(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC))
	sink := NewMemorySink(0)
	exec := NewSimExecutor(clk)
	s, err := New(Config{Capacity: cap, Clock: clk, Executor: exec, Sink: sink})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	s.Pause()
	s.Start()
	t.Cleanup(func() { _ = s.Close() })
	return &testEnv{s: s, clk: clk, sink: sink}
}

// batch runs fn while scheduling is paused, then resumes and settles. Use it
// around a group of submits that must be treated as arriving at the same
// instant (hand-computed initial queues).
func (e *testEnv) batch(fn func()) {
	fn()
	e.s.Resume()
	e.settle()
}

func (e *testEnv) tenant(t *testing.T, id string, weight int64) {
	t.Helper()
	if err := e.s.AddTenant(id, weight); err != nil {
		t.Fatalf("AddTenant(%s): %v", id, err)
	}
}

func (e *testEnv) submit(t *testing.T, id, tenant string, cpu, mem int64, d time.Duration) {
	t.Helper()
	_, err := e.s.Submit(SubmitRequest{ID: id, TenantID: tenant, CPU: cpu, Memory: mem, Duration: d})
	if err != nil {
		t.Fatalf("Submit(%s): %v", id, err)
	}
}

// settle lets the asynchronous scheduling goroutine finish all currently
// possible transitions (nothing is time-driven until clk.Advance is called).
func (e *testEnv) settle() {
	if err := e.s.WaitQuiet(2*time.Second, 30*time.Millisecond); err != nil {
		panic("settle: " + err.Error())
	}
}

// startedSeq returns task ids in TASK_STARTED event order.
func (e *testEnv) startedSeq() []string {
	var out []string
	for _, ev := range e.sink.Events() {
		if ev.Type == EventStarted {
			out = append(out, ev.TaskID)
		}
	}
	return out
}

func (e *testEnv) stateOf(id string) TaskState {
	v, err := e.s.GetTask(id)
	if err != nil {
		return ""
	}
	return v.State
}

// assertNoOvercommit checks the no-over-allocation invariant for every
// STARTED/FINISHED event in the log and the current snapshot.
func (e *testEnv) assertNoOvercommit(t *testing.T) {
	t.Helper()
	used := Resources{}
	for _, ev := range e.sink.Events() {
		switch ev.Type {
		case EventStarted:
			used = used.Add(ev.Resources)
		case EventFinished:
			// Cancelled (never-started) events release nothing.
			if st, _ := e.s.GetTask(ev.TaskID); st.State == StateFailed && st.StartedAt == nil {
				continue
			}
			if ev.Detail["status"] != "CANCELLED" {
				used = used.Sub(ev.Resources)
			}
		}
		if !used.LessEqual(e.s.capacity) {
			t.Fatalf("overcommit after event %d (%s %s): used=%v capacity=%v",
				ev.Seq, ev.Type, ev.TaskID, used, e.s.capacity)
		}
	}
	snap := e.s.Snapshot()
	if !snap.Used.LessEqual(snap.Capacity) {
		t.Fatalf("snapshot overcommit: used=%v cap=%v", snap.Used, snap.Capacity)
	}
	if !snap.Invariant.UsedFitsCapacity {
		t.Fatalf("invariant report says overcommit")
	}
}

// ---------------------------------------------------------------------------
// Scenario 1: the classic DRF hand-computed example (Ghodsi et al.).
//
// Capacity = 9 CPU, 18 GB memory.
// Tenant A tasks request <1 CPU, 4 GB>  (dominant = memory, 2/9 each)
// Tenant B tasks request <3 CPU, 1 GB>  (dominant = CPU,    3/9 each)
//
// Hand-computed start order when A1..A4 and B1..B3 are all queued at t0,
// canonical DRF (compare tenants' CURRENT dominant share; request only
// decides feasibility; zero/zero tie broken by smaller tenant id):
//
//  1. A1  both at 0, tie -> A;            A dom 2/9
//  2. B1  B 0 < A 2/9;                    B dom 3/9
//  3. A2  A 2/9 < B 3/9;                  A dom 4/9
//  4. B2  B 3/9 < A 4/9;                  B dom 6/9
//  5. A3  A 4/9 < B 6/9;                  A dom 6/9
//
// Then A4 would take cluster CPU to 10>9 and B3 to 12>9: both WAIT
// (non-preemptive). Final split is 3 A tasks : 2 B tasks, the DRF ratio.
// ---------------------------------------------------------------------------
func TestClassicDRFHandComputed(t *testing.T) {
	env := newTestEnv(t, Resources{CPU: 9000, Memory: 18000}) // milli-units
	env.tenant(t, "A", 1)
	env.tenant(t, "B", 1)

	env.batch(func() {
		env.submit(t, "A1", "A", 1000, 4000, 100*time.Millisecond)
		env.submit(t, "A2", "A", 1000, 4000, 100*time.Millisecond)
		env.submit(t, "A3", "A", 1000, 4000, 100*time.Millisecond)
		env.submit(t, "A4", "A", 1000, 4000, 100*time.Millisecond)
		env.submit(t, "B1", "B", 3000, 1000, 200*time.Millisecond)
		env.submit(t, "B2", "B", 3000, 1000, 50*time.Millisecond)
		env.submit(t, "B3", "B", 3000, 1000, 200*time.Millisecond)
	})

	got := env.startedSeq()
	want := []string{"A1", "B1", "A2", "B2", "A3"}
	if len(got) != len(want) {
		t.Fatalf("start order = %v, want %v (A4/B3 must wait)", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("start order = %v, want %v", got, want)
		}
	}
	if env.stateOf("A4") != StateQueued || env.stateOf("B3") != StateQueued {
		t.Fatalf("A4=%s B3=%s, both must be QUEUED", env.stateOf("A4"), env.stateOf("B3"))
	}
	snap := env.s.Snapshot()
	if snap.Used != (Resources{CPU: 9000, Memory: 14000}) {
		t.Fatalf("used = %v, want {9000 14000}", snap.Used)
	}
	env.assertNoOvercommit(t)

	// --- Release sequence, continued hand computation ---------------------
	// t=50ms: B2 finishes (used 6 CPU / 13 mem). Current dominant shares are
	// A 6/9, B 3/9, so B3 starts. A4 still cannot fit (B would hold 6 CPU,
	// 6+3+1=10>9).
	env.clk.Advance(50 * time.Millisecond)
	env.settle()
	if env.stateOf("B2") != StateComplete || env.stateOf("B3") != StateRunning {
		t.Fatalf("t=50ms: B2=%s B3=%s, want COMPLETE/RUNNING", env.stateOf("B2"), env.stateOf("B3"))
	}
	if env.stateOf("A4") != StateQueued {
		t.Fatalf("t=50ms: A4=%s, must still wait (CPU 9+1>9 with B at 6)", env.stateOf("A4"))
	}

	// t=100ms: A1,A2,A3 finish together (used 6 CPU / 2 mem: B1+B3). A4 now
	// fits (7<=9) and starts; it is A's queued head.
	env.clk.Advance(50 * time.Millisecond)
	env.settle()
	for _, id := range []string{"A1", "A2", "A3"} {
		if env.stateOf(id) != StateComplete {
			t.Fatalf("t=100ms: %s=%s, want COMPLETE", id, env.stateOf(id))
		}
	}
	if env.stateOf("A4") != StateRunning {
		t.Fatalf("t=100ms: A4=%s, want RUNNING", env.stateOf("A4"))
	}
	if env.stateOf("B3") != StateRunning {
		t.Fatalf("t=100ms: B3=%s, want RUNNING", env.stateOf("B3"))
	}
	env.assertNoOvercommit(t)

	// Run to completion (A4 ends 200ms virtual, B1 200ms, B3 250ms) and
	// verify the cluster drains without overcommit.
	env.clk.Advance(500 * time.Millisecond)
	if err := env.s.WaitIdle(2 * time.Second); err != nil {
		t.Fatal(err)
	}
	for _, id := range []string{"A1", "A2", "A3", "A4", "B1", "B2", "B3"} {
		if env.stateOf(id) != StateComplete {
			t.Fatalf("%s=%s, want COMPLETE at end", id, env.stateOf(id))
		}
	}
	if used := env.s.Snapshot().Used; used != (Resources{}) {
		t.Fatalf("final used = %v, want zero", used)
	}
	env.assertNoOvercommit(t)
}

// ---------------------------------------------------------------------------
// Scenario 2: a big task occupying the whole cluster blocks small tasks; they
// start only after resources are released (never by preemption).
// ---------------------------------------------------------------------------
func TestBigTaskBlocksSmallTasks(t *testing.T) {
	env := newTestEnv(t, Resources{CPU: 4000, Memory: 4000})
	env.tenant(t, "G", 1)
	env.tenant(t, "X", 1)
	env.tenant(t, "Y", 1)

	env.batch(func() {
		env.submit(t, "big", "G", 4000, 4000, 100*time.Millisecond)
	})
	if env.stateOf("big") != StateRunning {
		t.Fatalf("big = %s, want RUNNING", env.stateOf("big"))
	}
	env.batch(func() {
		env.submit(t, "X1", "X", 1000, 1000, 100*time.Millisecond)
		env.submit(t, "Y1", "Y", 1000, 1000, 100*time.Millisecond)
	})

	if env.stateOf("X1") != StateQueued || env.stateOf("Y1") != StateQueued {
		t.Fatalf("X1=%s Y1=%s, both must wait behind non-preemptible big task",
			env.stateOf("X1"), env.stateOf("Y1"))
	}
	waitingReasons := map[string]string{}
	for _, ev := range env.sink.Events() {
		if ev.Type == EventWaiting {
			waitingReasons[ev.TaskID] = ev.Detail["reason"].(string)
		}
	}
	if waitingReasons["X1"] != "INSUFFICIENT_RESOURCES" ||
		waitingReasons["Y1"] != "INSUFFICIENT_RESOURCES" {
		t.Fatalf("waiting reasons = %v, want INSUFFICIENT_RESOURCES", waitingReasons)
	}
	env.assertNoOvercommit(t)

	// Release: big finishes at t=100ms; both small tasks start in one pass,
	// tie broken by tenant id (X before Y).
	env.clk.Advance(100 * time.Millisecond)
	env.settle()
	if env.stateOf("big") != StateComplete {
		t.Fatalf("big = %s, want COMPLETE", env.stateOf("big"))
	}
	if env.stateOf("X1") != StateRunning || env.stateOf("Y1") != StateRunning {
		t.Fatalf("after release X1=%s Y1=%s, want RUNNING", env.stateOf("X1"), env.stateOf("Y1"))
	}
	if seq := env.startedSeq(); seq[len(seq)-2] != "X1" || seq[len(seq)-1] != "Y1" {
		t.Fatalf("post-release start order tail = %v, want X1,Y1", seq)
	}
	env.assertNoOvercommit(t)

	env.clk.Advance(200 * time.Millisecond)
	if err := env.s.WaitIdle(2 * time.Second); err != nil {
		t.Fatal(err)
	}
	env.assertNoOvercommit(t)
}

// ---------------------------------------------------------------------------
// Scenario 3: small tasks keep arriving while the cluster is full. A1 holds
// CPU/mem for 10ms, B1 for 5ms; B2 and A2 arrive at t=3ms. At t=5ms B1 frees
// resources and B2 (zero share) is preferred over A2 (A already holds half);
// A2 starts at t=10ms when A1 finishes.
// ---------------------------------------------------------------------------
func TestSmallTasksArrivingContinuously(t *testing.T) {
	env := newTestEnv(t, Resources{CPU: 2000, Memory: 2000})
	env.tenant(t, "A", 1)
	env.tenant(t, "B", 1)

	env.batch(func() {
		env.submit(t, "A1", "A", 1000, 1000, 10*time.Millisecond)
		env.submit(t, "B1", "B", 1000, 1000, 5*time.Millisecond)
	})
	if seq := env.startedSeq(); len(seq) != 2 || seq[0] != "A1" || seq[1] != "B1" {
		t.Fatalf("initial starts = %v, want A1,B1", seq)
	}

	env.clk.Advance(3 * time.Millisecond)
	env.batch(func() {
		env.submit(t, "A2", "A", 1000, 1000, 10*time.Millisecond)
		env.submit(t, "B2", "B", 1000, 1000, 10*time.Millisecond)
	})
	if env.stateOf("A2") != StateQueued || env.stateOf("B2") != StateQueued {
		t.Fatalf("at t=3ms A2=%s B2=%s, cluster full -> both QUEUED",
			env.stateOf("A2"), env.stateOf("B2"))
	}

	env.clk.Advance(2 * time.Millisecond) // t=5ms: B1 finishes
	env.settle()
	if env.stateOf("B1") != StateComplete || env.stateOf("B2") != StateRunning {
		t.Fatalf("t=5ms: B1=%s B2=%s, want COMPLETE/RUNNING", env.stateOf("B1"), env.stateOf("B2"))
	}
	if env.stateOf("A2") != StateQueued {
		t.Fatalf("A2 must keep waiting at t=5ms (A still holds half), got %s", env.stateOf("A2"))
	}

	env.clk.Advance(5 * time.Millisecond) // t=10ms: A1 finishes
	env.settle()
	if env.stateOf("A1") != StateComplete || env.stateOf("A2") != StateRunning {
		t.Fatalf("t=10ms: A1=%s A2=%s, want COMPLETE/RUNNING", env.stateOf("A1"), env.stateOf("A2"))
	}
	want := []string{"A1", "B1", "B2", "A2"}
	if seq := env.startedSeq(); len(seq) != 4 || seq[3] != "A2" {
		t.Fatalf("start order = %v, want %v", seq, want)
	}
	env.clk.Advance(20 * time.Millisecond)
	if err := env.s.WaitIdle(2 * time.Second); err != nil {
		t.Fatal(err)
	}
	env.assertNoOvercommit(t)
}

// ---------------------------------------------------------------------------
// Scenario 4: explicit weights. Capacity 10/10, equal <1,1> tasks, tenant A
// weight 1, tenant B weight 2. Selection key is (current dominant
// share)/weight; exact-fraction hand computation gives:
//
//	A1 B1 B2 A2 B3 B4 A3 B5 B6 A4
//
// (e.g. after A1,B1,B2 both weighted shares are 1/10 and the tie goes to A;
// after A3,B4 both are again 1/5 and the tie again goes to A.) Final split is
// 4 A tasks : 6 B tasks, the configured 1:2 weight ratio; A5/B7 wait.
// ---------------------------------------------------------------------------
func TestWeightedDRF(t *testing.T) {
	env := newTestEnv(t, Resources{CPU: 10, Memory: 10})
	env.tenant(t, "A", 1)
	env.tenant(t, "B", 2)
	env.batch(func() {
		for _, id := range []string{"A1", "A2", "A3", "A4", "A5"} {
			env.submit(t, id, "A", 1, 1, time.Hour)
		}
		for _, id := range []string{"B1", "B2", "B3", "B4", "B5", "B6", "B7"} {
			env.submit(t, id, "B", 1, 1, time.Hour)
		}
	})

	want := []string{"A1", "B1", "B2", "A2", "B3", "B4", "A3", "B5", "B6", "A4"}
	got := env.startedSeq()
	if len(got) != len(want) {
		t.Fatalf("started = %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("start order = %v\nwant        %v (first mismatch at %d: %s vs %s)",
				got, want, i, got[i], want[i])
		}
	}
	if env.stateOf("A5") != StateQueued || env.stateOf("B7") != StateQueued {
		t.Fatalf("A5=%s B7=%s, both must be QUEUED (cluster full: 4+6=10)",
			env.stateOf("A5"), env.stateOf("B7"))
	}
	snap := env.s.Snapshot()
	if snap.Used != (Resources{CPU: 10, Memory: 10}) {
		t.Fatalf("used = %v, want full {10 10}", snap.Used)
	}
	// Weighted shares: A = (4/10)/1 = 2/5; B = (6/10)/2 = 3/10.
	for _, tn := range snap.Tenants {
		switch tn.ID {
		case "A":
			if tn.WeightedShare != "2/5" {
				t.Fatalf("A weighted share = %s, want 2/5", tn.WeightedShare)
			}
		case "B":
			if tn.WeightedShare != "3/10" {
				t.Fatalf("B weighted share = %s, want 3/10", tn.WeightedShare)
			}
		}
	}
	env.assertNoOvercommit(t)
}

// Determinism: the same submission pattern produces the identical sequence
// when replayed on a fresh scheduler.
func TestDeterministicReplay(t *testing.T) {
	run := func() []string {
		env := newTestEnv(t, Resources{CPU: 6000, Memory: 6000})
		env.tenant(t, "alpha", 1)
		env.tenant(t, "beta", 3)
		env.batch(func() {
			env.submit(t, "alpha-1", "alpha", 2000, 2000, time.Hour)
			env.submit(t, "alpha-2", "alpha", 2000, 2000, time.Hour)
			env.submit(t, "beta-1", "beta", 2000, 2000, time.Hour)
			env.submit(t, "beta-2", "beta", 2000, 2000, time.Hour)
			env.submit(t, "beta-3", "beta", 2000, 2000, time.Hour)
		})
		return env.startedSeq()
	}
	first := run()
	second := run()
	if len(first) == 0 {
		t.Fatal("no tasks started")
	}
	for i := range first {
		if first[i] != second[i] {
			t.Fatalf("non-deterministic order:\nfirst  %v\nsecond %v", first, second)
		}
	}
}

// Per-tenant FIFO: a large head task that does not fit blocks later tasks of
// the SAME tenant even if those later tasks are tiny; other tenants proceed.
func TestPerTenantFIFOHeadBlocking(t *testing.T) {
	env := newTestEnv(t, Resources{CPU: 4000, Memory: 4000})
	env.tenant(t, "A", 1)
	env.tenant(t, "C", 1)

	// A-head is huge (whole cluster); A-tail and C's small task are submitted
	// at the same instant. DRF picks the zero-share tie in tenant order: A-big
	// starts first and fills the cluster, so A-small and C1 both wait.
	env.batch(func() {
		env.submit(t, "A-big", "A", 4000, 4000, time.Hour)
		env.submit(t, "A-small", "A", 100, 100, 10*time.Millisecond)
		env.submit(t, "C1", "C", 1000, 1000, 10*time.Millisecond)
	})

	if env.stateOf("A-big") != StateRunning {
		t.Fatalf("A-big = %s, want RUNNING", env.stateOf("A-big"))
	}
	if env.stateOf("A-small") != StateQueued || env.stateOf("C1") != StateQueued {
		t.Fatalf("A-small=%s C1=%s, cluster full -> QUEUED", env.stateOf("A-small"), env.stateOf("C1"))
	}
	// A-small is behind the same-tenant head? No: A-big is already RUNNING, so
	// A-small IS the queued head and waits for resources. Its waiting reason
	// must be INSUFFICIENT_RESOURCES, not a FIFO block.
	for _, ev := range env.sink.Events() {
		if ev.Type == EventWaiting && ev.TaskID == "A-small" {
			if ev.Detail["reason"] != "INSUFFICIENT_RESOURCES" {
				t.Fatalf("A-small wait reason = %v", ev.Detail)
			}
		}
	}

	// Now demonstrate true same-tenant FIFO blocking: make A's queued head
	// larger than free capacity while a later same-tenant task would fit.
	env2 := newTestEnv(t, Resources{CPU: 3000, Memory: 3000})
	env2.tenant(t, "A", 1)
	env2.tenant(t, "C", 1)
	env2.batch(func() {
		env2.submit(t, "run1", "C", 2000, 2000, time.Hour) // occupy 2/3
	})
	env2.batch(func() {
		env2.submit(t, "A-big", "A", 2000, 2000, time.Hour) // needs 2, only 1 free
		env2.submit(t, "A-tiny", "A", 500, 500, time.Hour)  // would fit, but FIFO
	})
	if env2.stateOf("A-big") != StateQueued || env2.stateOf("A-tiny") != StateQueued {
		t.Fatalf("A-big=%s A-tiny=%s, both QUEUED", env2.stateOf("A-big"), env2.stateOf("A-tiny"))
	}
	var tinyReason string
	for _, ev := range env2.sink.Events() {
		if ev.Type == EventWaiting && ev.TaskID == "A-tiny" {
			tinyReason = ev.Detail["reason"].(string)
		}
	}
	if tinyReason != "TENANT_QUEUE_FULL" {
		t.Fatalf("A-tiny reason = %q, want TENANT_QUEUE_FULL (FIFO head blocks)", tinyReason)
	}
}

// Memory can be the dominant dimension. Capacity 10/10; tenant C tasks are
// CPU-heavy <4 CPU, 1 mem>, tenant M tasks memory-heavy <1 CPU, 4 mem>.
//
// Hand computation with C1..C3 and M1,M2 all queued at t0 (equal weight):
//
//	C1  zero/zero tie, tenant id C<M;                     used <4,1>  C dom .4
//	M1  M projects mem-dom .4 < C2's cpu-dom .5;          used <5,5>
//	C2  C projects cpu-dom .9, M2 projects mem-dom .9 tie -> id C; used <9,6>
//	M2  fits exactly (<10,10>);                           used <10,10>
//	C3  would need CPU 14>10: WAIT.
func TestMemoryDominantDimension(t *testing.T) {
	env := newTestEnv(t, Resources{CPU: 10000, Memory: 10000})
	env.tenant(t, "C", 1) // cpu-heavy <4 CPU, 1 mem>
	env.tenant(t, "M", 1) // memory-heavy <1 CPU, 4 mem>

	env.batch(func() {
		env.submit(t, "C1", "C", 4000, 1000, time.Hour)
		env.submit(t, "C2", "C", 4000, 1000, time.Hour)
		env.submit(t, "C3", "C", 4000, 1000, time.Hour)
		env.submit(t, "M1", "M", 1000, 4000, time.Hour)
		env.submit(t, "M2", "M", 1000, 4000, time.Hour)
	})

	want := []string{"C1", "M1", "C2", "M2"}
	got := env.startedSeq()
	if len(got) != len(want) {
		t.Fatalf("started = %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("start order = %v, want %v", got, want)
		}
	}
	if env.stateOf("C3") != StateQueued {
		t.Fatalf("C3 = %s, want QUEUED (cluster exactly full)", env.stateOf("C3"))
	}
	snap := env.s.Snapshot()
	if snap.Used != (Resources{CPU: 10000, Memory: 10000}) {
		t.Fatalf("used = %v, want {10000 10000}", snap.Used)
	}
	// Each tenant's own dominant share is 8/10 (C: CPU 8, M: mem 8).
	for _, tn := range snap.Tenants {
		if tn.DominantShare != "4/5" {
			t.Fatalf("tenant %s dominant share = %s, want 4/5", tn.ID, tn.DominantShare)
		}
	}
	env.assertNoOvercommit(t)
}

// Submission-time validation and capacity growth unblock a waiting big task.
func TestValidationAndCapacityGrowth(t *testing.T) {
	env := newTestEnv(t, Resources{CPU: 1000, Memory: 1000})
	env.tenant(t, "A", 1)

	if _, err := env.s.Submit(SubmitRequest{TenantID: "A", CPU: 2000, Memory: 1, Duration: time.Second}); !errorIs(err, ErrCapacity) {
		t.Fatalf("oversized CPU task: err=%v, want ErrCapacity", err)
	}
	if _, err := env.s.Submit(SubmitRequest{TenantID: "ghost", CPU: 1, Memory: 1, Duration: time.Second}); !errorIs(err, ErrUnknownTenant) {
		t.Fatalf("unknown tenant: err=%v, want ErrUnknownTenant", err)
	}
	if _, err := env.s.Submit(SubmitRequest{TenantID: "A", CPU: 0, Memory: 1, Duration: time.Second}); !errorIs(err, ErrBadRequest) {
		t.Fatalf("zero cpu: err=%v, want ErrBadRequest", err)
	}
	if err := env.s.AddTenant("B", 0); !errorIs(err, ErrBadRequest) {
		t.Fatalf("weight 0: err=%v, want ErrBadRequest", err)
	}

	// Big task fills the cluster, a small one waits; growing capacity unblocks.
	env.batch(func() {
		env.submit(t, "big", "A", 1000, 1000, time.Hour)
	})
	env.submit(t, "small", "A", 500, 500, time.Hour)
	env.settle()
	if env.stateOf("small") != StateQueued {
		t.Fatalf("small = %s, want QUEUED", env.stateOf("small"))
	}
	if err := env.s.SetCapacity(Resources{CPU: 900, Memory: 1000}); !errorIs(err, ErrCapacity) {
		t.Fatalf("shrink below used cpu: err=%v, want ErrCapacity", err)
	}
	if err := env.s.SetCapacity(Resources{CPU: 2000, Memory: 2000}); err != nil {
		t.Fatalf("grow capacity: %v", err)
	}
	env.settle()
	if env.stateOf("small") != StateRunning {
		t.Fatalf("after capacity growth small = %s, want RUNNING", env.stateOf("small"))
	}
	env.assertNoOvercommit(t)
}

// Cancelling a queued task removes it and frees nothing; cancelling is
// recorded as a structured terminal event.
func TestCancelQueuedTask(t *testing.T) {
	env := newTestEnv(t, Resources{CPU: 1000, Memory: 1000})
	env.tenant(t, "A", 1)
	env.tenant(t, "B", 1)
	env.batch(func() {
		env.submit(t, "big", "A", 1000, 1000, time.Hour)
		env.submit(t, "small", "B", 500, 500, time.Hour)
	})
	if env.stateOf("small") != StateQueued {
		t.Fatalf("small = %s, want QUEUED", env.stateOf("small"))
	}
	if err := env.s.CancelTask("small", "user request"); err != nil {
		t.Fatalf("cancel: %v", err)
	}
	v, _ := env.s.GetTask("small")
	if v.State != StateFailed || v.FailMsg == "" {
		t.Fatalf("cancelled task = %+v, want FAILED with reason", v)
	}
	var sawCancel bool
	for _, ev := range env.sink.Events() {
		if ev.Type == EventFinished && ev.TaskID == "small" && ev.Detail["status"] == "CANCELLED" {
			sawCancel = true
		}
	}
	if !sawCancel {
		t.Fatal("no CANCELLED finish event recorded")
	}
	env.assertNoOvercommit(t)
}
