package scheduler

import (
	"testing"
	"time"
)

// startFinishOrder extracts ordered task ids for TASK_STARTED / TASK_FINISHED events.
func startFinishOrder(events []Event) (starts, finishes []string) {
	for _, ev := range events {
		switch ev.Type {
		case EventTaskStarted:
			starts = append(starts, ev.TaskID)
		case EventTaskFinished:
			finishes = append(finishes, ev.TaskID)
		}
	}
	return starts, finishes
}

func assertNoOvercommit(t *testing.T, events []Event, cap Resources, stage string) {
	t.Helper()
	for _, ev := range events {
		if ev.Used != nil && (ev.Used.CPU > cap.CPU || ev.Used.Mem > cap.Mem) {
			t.Fatalf("[%s] overcommit after event %d (%s/%s): used=%v cap=%v",
				stage, ev.EventID, ev.Type, ev.TaskID, *ev.Used, cap)
		}
		if ev.Available != nil && (ev.Available.CPU < 0 || ev.Available.Mem < 0) {
			t.Fatalf("[%s] negative availability after event %d: %v", stage, ev.EventID, *ev.Available)
		}
	}
}

func statusOf(t *testing.T, snap *ClusterSnapshot, id string) TaskStatus {
	t.Helper()
	for _, tk := range snap.Tasks {
		if tk.ID == id {
			return tk.Status
		}
	}
	t.Fatalf("task %s not found in snapshot", id)
	return ""
}

func tenantView(snap *ClusterSnapshot, id string) *TenantView {
	for i := range snap.Tenants {
		if snap.Tenants[i].ID == id {
			return &snap.Tenants[i]
		}
	}
	return nil
}

// TestAcceptanceHandCalculated is the acceptance scenario worked out by
// hand in README.md (capacity 10 CPU / 10 MiB, three equal-weight tenants).
//
// Timeline of submissions (clock ticks are seconds):
//
//	t=0: T1 A(6,1) d2 ; T2 A(6,6) d3 ; T3 B(4,9) d5 ; T4 C(9,9) d5
//	     T5 B(1,1) d1 ; T6 B(2,2) d3 ; T7 C(1,1) d3
//	t=1: T8 B(1,1) d1
//
// Hand-derived result:
//
//	t=0 starts T1,T3; T2 blocks A (mem), T4 blocks C (both dims);
//	     B's small T5 waits behind running T3 only.
//	t=2 T1 ends -> T5 starts (big T2 still cannot fit 1 MiB avail).
//	t=3 T5 ends -> nothing fits (avail 6 CPU / 1 MiB).
//	t=5 T3 ends -> T2, T6 and T8 start (each pass re-sorts on equal
//	     shares; alphabetical tenant tiebreak gives A then B, and B's
//	     FIFO queue serves both fitting tasks).
//	t=6 T8 ends (1/1 freed) but C head T4 (9,9) still does not fit, so
//	     T7 stays blocked behind its own tenant's head.
//	t=8 T2 then T6 end (same deadline, insertion order): T4 starts only
//	     after both release, then T7 starts in the same pass (rem. 1/1).
//	t=11 T7 ends; t=13 T4 ends; cluster empty.
func TestAcceptanceHandCalculated(t *testing.T) {
	clock := NewFakeClock(time.Unix(0, 0).UTC())
	store := NewMemoryStore()
	sched, err := New(Resources{CPU: 10, Mem: 10},
		WithClock(clock),
		WithExecutor(NewTimedExecutor(clock)),
		WithEventStore(store),
	)
	if err != nil {
		t.Fatal(err)
	}
	for _, id := range []string{"A", "B", "C"} {
		if err := sched.AddTenant(id, 1); err != nil {
			t.Fatal(err)
		}
	}

	submit := func(id, tenant string, cpu, mem int64, d time.Duration) {
		t.Helper()
		if err := sched.Submit(TaskSpec{
			ID: id, TenantID: tenant,
			Request:  Resources{CPU: cpu, Mem: mem},
			Duration: Duration{Duration: d},
		}); err != nil {
			t.Fatalf("submit %s: %v", id, err)
		}
	}

	// t=0 batch
	submit("T1", "A", 6, 1, 2*time.Second)
	submit("T2", "A", 6, 6, 3*time.Second)
	submit("T3", "B", 4, 9, 5*time.Second)
	submit("T4", "C", 9, 9, 5*time.Second)
	submit("T5", "B", 1, 1, 1*time.Second)
	submit("T6", "B", 2, 2, 3*time.Second)
	submit("T7", "C", 1, 1, 3*time.Second)

	ev0 := sched.Events(0, 0)
	assertNoOvercommit(t, ev0, sched.Capacity(), "t=0")

	snap := sched.Snapshot()
	if snap.Used != (Resources{CPU: 10, Mem: 10}) {
		t.Fatalf("t=0 used = %v, want {10 10}", snap.Used)
	}
	if got := statusOf(t, snap, "T1"); got != StatusRunning {
		t.Fatalf("T1 at t=0 = %s", got)
	}
	if got := statusOf(t, snap, "T3"); got != StatusRunning {
		t.Fatalf("T3 at t=0 = %s", got)
	}
	for _, id := range []string{"T2", "T4", "T5", "T6", "T7"} {
		if got := statusOf(t, snap, id); got != StatusWaiting {
			t.Fatalf("%s at t=0 = %s, want WAITING", id, got)
		}
	}
	if got := waitingIDs(tenantView(snap, "B")); !equalStrings(got, []string{"T5", "T6"}) {
		t.Fatalf("B waiting at t=0 = %v, want [T5 T6]", got)
	}
	if got := waitingIDs(tenantView(snap, "C")); !equalStrings(got, []string{"T4", "T7"}) {
		t.Fatalf("C waiting at t=0 = %v, want [T4 T7]", got)
	}

	// t=1: small task keeps arriving while the cluster is full.
	clock.Advance(1 * time.Second)
	submit("T8", "B", 1, 1, 1*time.Second)
	if got := statusOf(t, sched.Snapshot(), "T8"); got != StatusWaiting {
		t.Fatalf("T8 at t=1 = %s, want WAITING", got)
	}

	advance := func(tick time.Duration, stage string) {
		t.Helper()
		before := int64(len(sched.Events(0, 0)))
		clock.Advance(tick)
		assertNoOvercommit(t, sched.Events(before, 0), sched.Capacity(), stage)
	}

	// t=2: T1 finishes, T5 (B small) must win the freed 6 CPU / 1 MiB.
	advance(1*time.Second, "t=2")
	snap = sched.Snapshot()
	if statusOf(t, snap, "T1") != StatusDone || statusOf(t, snap, "T5") != StatusRunning {
		t.Fatalf("at t=2: T1=%v T5=%v", statusOf(t, snap, "T1"), statusOf(t, snap, "T5"))
	}
	if snap.Used != (Resources{CPU: 5, Mem: 10}) {
		t.Fatalf("t=2 used = %v, want {5 10}", snap.Used)
	}
	if statusOf(t, snap, "T2") != StatusWaiting {
		t.Fatal("T2 must remain blocked at t=2")
	}

	// t=3: T5 finishes; nothing else fits (avail 6/1).
	advance(1*time.Second, "t=3")
	snap = sched.Snapshot()
	if snap.Used != (Resources{CPU: 4, Mem: 9}) {
		t.Fatalf("t=3 used = %v, want {4 9}", snap.Used)
	}
	if statusOf(t, snap, "T6") != StatusWaiting || statusOf(t, snap, "T8") != StatusWaiting {
		t.Fatal("B small tasks must still wait at t=3 (no memory)")
	}

	// t=4: nothing happens.
	advance(1*time.Second, "t=4")
	if got := sched.Snapshot().Used; got != (Resources{CPU: 4, Mem: 9}) {
		t.Fatalf("t=4 used = %v, want unchanged {4 9}", got)
	}

	// t=5: T3 frees everything. Equal-share tiebreak A before B, and B's
	// FIFO queue serves both fitting small tasks: T2, T6, T8 start.
	advance(1*time.Second, "t=5")
	snap = sched.Snapshot()
	if snap.Used != (Resources{CPU: 9, Mem: 9}) {
		t.Fatalf("t=5 used = %v, want {9 9}", snap.Used)
	}
	if statusOf(t, snap, "T2") != StatusRunning ||
		statusOf(t, snap, "T6") != StatusRunning ||
		statusOf(t, snap, "T8") != StatusRunning {
		t.Fatalf("at t=5: T2=%v T6=%v T8=%v",
			statusOf(t, snap, "T2"), statusOf(t, snap, "T6"), statusOf(t, snap, "T8"))
	}
	if statusOf(t, snap, "T3") != StatusDone {
		t.Fatal("T3 must be finished at t=5")
	}
	if statusOf(t, snap, "T4") != StatusWaiting {
		t.Fatal("T4 (9,9) must still wait: only 1/1 free at t=5")
	}
	if statusOf(t, snap, "T7") != StatusWaiting {
		t.Fatal("T7 must still wait: only 1/1 free at t=5")
	}

	// t=6: T8 frees 1/1. C's head is the big T4 (9,9) which still does
	// not fit; per-tenant FIFO means T7 is blocked behind T4 and must
	// wait even though 1/1 would fit it. Demonstrates head-of-line
	// blocking within a tenant queue.
	advance(1*time.Second, "t=6")
	snap = sched.Snapshot()
	if statusOf(t, snap, "T8") != StatusDone {
		t.Fatal("T8 must finish at t=6")
	}
	if statusOf(t, snap, "T4") != StatusWaiting || statusOf(t, snap, "T7") != StatusWaiting {
		t.Fatalf("at t=6: T4=%v T7=%v, both must WAIT behind C head",
			statusOf(t, snap, "T4"), statusOf(t, snap, "T7"))
	}
	cv := tenantView(snap, "C")
	if cv == nil || !cv.HeadBlocked {
		t.Fatal("tenant C should report head blocked by current availability")
	}
	if snap.Used != (Resources{CPU: 8, Mem: 8}) {
		t.Fatalf("t=6 used = %v, want {8 8}", snap.Used)
	}

	// t=7: nothing.
	advance(1*time.Second, "t=7")
	if got := sched.Snapshot().Used; got != (Resources{CPU: 8, Mem: 8}) {
		t.Fatalf("t=7 used = %v, want {8 8}", got)
	}

	// t=8: T2 and T6 share a deadline; T2's timer was inserted first so
	// it completes first, then T6. Both must release before T4 (9,9) fits;
	// once T4 starts, T7 becomes C's head and the remaining 1/1 lets it
	// start in the same scheduling pass.
	advance(1*time.Second, "t=8")
	snap = sched.Snapshot()
	if statusOf(t, snap, "T2") != StatusDone || statusOf(t, snap, "T6") != StatusDone {
		t.Fatalf("at t=8: T2=%v T6=%v, want both FINISHED",
			statusOf(t, snap, "T2"), statusOf(t, snap, "T6"))
	}
	if statusOf(t, snap, "T4") != StatusRunning {
		t.Fatalf("T4 at t=8 = %s, want RUNNING", statusOf(t, snap, "T4"))
	}
	if statusOf(t, snap, "T7") != StatusRunning {
		t.Fatalf("T7 at t=8 = %s, want RUNNING behind T4 within same pass",
			statusOf(t, snap, "T7"))
	}
	if snap.Used != (Resources{CPU: 10, Mem: 10}) {
		t.Fatalf("t=8 used = %v, want {10 10} (T4 9/9 + T7 1/1)", snap.Used)
	}

	// t=9, t=10 nothing.
	advance(1*time.Second, "t=9")
	advance(1*time.Second, "t=10")
	if got := sched.Snapshot().Used; got != (Resources{CPU: 10, Mem: 10}) {
		t.Fatalf("t=10 used = %v, want {10 10}", got)
	}

	// t=11: T7 (d=3, started at t=8) finishes.
	advance(1*time.Second, "t=11")
	snap = sched.Snapshot()
	if statusOf(t, snap, "T7") != StatusDone {
		t.Fatal("T7 must finish at t=11")
	}
	if snap.Used != (Resources{CPU: 9, Mem: 9}) {
		t.Fatalf("t=11 used = %v, want {9 9}", snap.Used)
	}

	// t=12: nothing; T4 is non-preemptible and holds until t=13.
	advance(1*time.Second, "t=12")
	if got := sched.Snapshot().Used; got != (Resources{CPU: 9, Mem: 9}) {
		t.Fatalf("t=12 used = %v, want {9 9}", got)
	}

	advance(1*time.Second, "t=13")
	snap = sched.Snapshot()
	if snap.Used != (Resources{CPU: 0, Mem: 0}) {
		t.Fatalf("t=13 used = %v, want cluster empty", snap.Used)
	}
	for _, tk := range snap.Tasks {
		if tk.Status != StatusDone {
			t.Fatalf("task %s status %s at t=13, want FINISHED", tk.ID, tk.Status)
		}
	}

	// Deterministic global ordering, matching the hand derivation.
	allEvents := sched.Events(0, 0)
	assertNoOvercommit(t, allEvents, sched.Capacity(), "final")
	starts, finishes := startFinishOrder(allEvents)
	wantStarts := []string{"T1", "T3", "T5", "T2", "T6", "T8", "T4", "T7"}
	if !equalStrings(starts, wantStarts) {
		t.Fatalf("start order = %v, want %v", starts, wantStarts)
	}
	wantFinishes := []string{"T1", "T5", "T3", "T8", "T2", "T6", "T7", "T4"}
	if !equalStrings(finishes, wantFinishes) {
		t.Fatalf("finish order = %v, want %v", finishes, wantFinishes)
	}
}

func waitingIDs(tv *TenantView) []string {
	var out []string
	for _, tk := range tv.Waiting {
		out = append(out, tk.ID)
	}
	return out
}

func equalStrings(a, b []string) bool {
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
