package core

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"deadlockcheck/internal/evidence"
	"deadlockcheck/internal/store"
	"deadlockcheck/internal/testutil"
)

func testService(t *testing.T) (*Service, context.Context) {
	t.Helper()
	dsn := testutil.IsolatedDSN(t)
	ctx := context.Background()
	pool := testutil.NewPool(t, dsn)
	if err := pool.Ping(ctx); err != nil {
		t.Skipf("postgres not reachable: %v", err)
	}
	st := &store.Store{Pool: pool}
	if err := st.Migrate(ctx); err != nil {
		t.Fatalf("migrate: %v", err)
	}

	key, err := evidence.GenerateKey()
	if err != nil {
		t.Fatalf("key: %v", err)
	}
	raw, err := evidence.ParseKey(key)
	if err != nil {
		t.Fatalf("parse key: %v", err)
	}
	return NewService(pool, evidence.NewSigner(raw), 1.0), ctx
}

func mustRegister(t *testing.T, ctx context.Context, svc *Service, refs ...Ref) {
	t.Helper()
	if err := svc.RegisterResources(ctx, refs, nil); err != nil {
		t.Fatalf("register %v: %v", refs, err)
	}
}

func mkRefs(pairs ...string) []Ref {
	out := make([]Ref, 0, len(pairs)/2)
	for i := 0; i < len(pairs); i += 2 {
		out = append(out, Ref{Kind: pairs[i], Name: pairs[i+1]})
	}
	return out
}

func mkTask(t *testing.T, ctx context.Context, svc *Service,
	label string, pri int, aging float64, timeoutMS int64, refs []Ref) *AllocationResult {
	t.Helper()
	res, err := svc.CreateTask(ctx, CreateTaskInput{
		Label:       label,
		Priority:    pri,
		AgingPerSec: aging,
		TimeoutMS:   timeoutMS,
		Resources:   refs,
	})
	if err != nil {
		t.Fatalf("create task %s: %v", label, err)
	}
	return res
}

func asCoreErr(t *testing.T, err error) *Error {
	t.Helper()
	var ce *Error
	if !errors.As(err, &ce) {
		t.Fatalf("expected *core.Error, got %T: %v", err, err)
	}
	return ce
}

// TestAtomicAcquireAndPartialFailure: a task that gets nothing must never
// hold anything, and a request that only partially fits is fully blocked.
func TestAtomicAcquireAndPartialFailure(t *testing.T) {
	svc, ctx := testService(t)
	mustRegister(t, ctx, svc,
		Ref{Kind: "tool", Name: "drill"},
		Ref{Kind: "station", Name: "bay-1"},
		Ref{Kind: "tool", Name: "welder"},
	)

	// A takes drill + bay-1 atomically.
	a := mkTask(t, ctx, svc, "A", 100, 0, 60000,
		mkRefs("tool", "drill", "station", "bay-1"))
	if !a.Granted || a.State != "running" || len(a.HeldResources) != 2 {
		t.Fatalf("A should immediately run with 2 holds, got %+v", a)
	}

	// B wants drill + welder: welder is free but drill is held -> B gets
	// nothing and must not hold welder.
	b := mkTask(t, ctx, svc, "B", 100, 0, 60000,
		mkRefs("tool", "drill", "tool", "welder"))
	if b.Granted || b.State != "waiting" {
		t.Fatalf("B must wait, got %+v", b)
	}
	if len(b.HeldResources) != 0 {
		t.Fatalf("B must hold NOTHING while waiting, holds %v", b.HeldResources)
	}
	if len(b.WaitReasons) != 1 || b.WaitReasons[0].HolderTaskID != a.TaskID {
		t.Fatalf("B should report exactly one blocker (A holds drill), got %+v", b.WaitReasons)
	}

	holds, err := svc.ListHolds(ctx)
	if err != nil {
		t.Fatal(err)
	}
	for _, h := range holds {
		if h.TaskID == b.TaskID {
			t.Fatalf("waiter B leaked a hold: %+v", h)
		}
	}
}

// TestCrossRequestsAndCycleRejection builds the classic AB-BA cross request
// via dynamic extra requests and proves the cycle-closing edge is refused.
func TestCrossRequestsAndCycleRejection(t *testing.T) {
	svc, ctx := testService(t)
	mustRegister(t, ctx, svc,
		Ref{Kind: "tool", Name: "X"},
		Ref{Kind: "tool", Name: "Y"},
	)

	a := mkTask(t, ctx, svc, "A", 100, 0, 60000, mkRefs("tool", "X"))
	b := mkTask(t, ctx, svc, "B", 100, 0, 60000, mkRefs("tool", "Y"))
	if !a.Granted || !b.Granted {
		t.Fatalf("A and B should each start with their own resource")
	}

	// A requests Y (held by B): A waits, edge A->B.
	ar, err := svc.SubmitExtra(ctx, a.TaskID, ExtraRequestInput{
		Resources: mkRefs("tool", "Y"),
	})
	if err != nil {
		t.Fatalf("A extra Y: %v", err)
	}
	if ar.Granted || ar.WaitReasons[0].HolderTaskID != b.TaskID {
		t.Fatalf("A should wait on B for Y, got %+v", ar)
	}

	// B requests X (held by A): this closes A->B->A and MUST be rejected
	// without disturbing B's existing hold on Y.
	_, err = svc.SubmitExtra(ctx, b.TaskID, ExtraRequestInput{
		Resources: mkRefs("tool", "X"),
	})
	if err == nil {
		t.Fatal("cycle-closing request must be rejected")
	}
	ce := asCoreErr(t, err)
	if ce.Kind != ErrCycle {
		t.Fatalf("expected cycle_detected, got %s: %v", ce.Kind, ce)
	}
	if len(ce.Details["cycle_task_ids"].([]int64)) < 2 {
		t.Fatalf("cycle path must be reported, details=%v", ce.Details)
	}

	// B still holds Y; graph has exactly one edge and no cycles.
	bv, err := svc.GetTask(ctx, b.TaskID)
	if err != nil {
		t.Fatal(err)
	}
	if len(bv.HeldResources) != 1 || bv.HeldResources[0].Name != "Y" {
		t.Fatalf("rejected cycle request must not alter B's holds, got %v", bv.HeldResources)
	}
	g, err := svc.WaitGraph(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if len(g.Cycles) != 0 {
		t.Fatalf("persistent graph must remain acyclic after rejection, cycles=%v", g.Cycles)
	}
	if len(g.Edges) != 1 || g.Edges[0].WaiterTaskID != a.TaskID || g.Edges[0].HolderTaskID != b.TaskID {
		t.Fatalf("expected single edge A->B, got %+v", g.Edges)
	}

	// Once B confirms completion and releases Y, the grant wave hands Y to A.
	if _, err := svc.Complete(ctx, b.TaskID); err != nil {
		t.Fatal(err)
	}
	av, err := svc.GetTask(ctx, a.TaskID)
	if err != nil {
		t.Fatal(err)
	}
	if av.State != "running" || len(av.HeldResources) != 2 {
		t.Fatalf("A should now run holding X+Y, got state=%s holds=%v", av.State, av.HeldResources)
	}
}

// TestThreeNodeCycle exercises a longer cross-resource ring.
func TestThreeNodeCycle(t *testing.T) {
	svc, ctx := testService(t)
	mustRegister(t, ctx, svc,
		mkRefs("tool", "R1", "tool", "R2", "tool", "R3")...,
	)
	a := mkTask(t, ctx, svc, "T1", 100, 0, 60000, mkRefs("tool", "R1"))
	b := mkTask(t, ctx, svc, "T2", 100, 0, 60000, mkRefs("tool", "R2"))
	c := mkTask(t, ctx, svc, "T3", 100, 0, 60000, mkRefs("tool", "R3"))

	waitExtra := func(taskID int64, ref Ref) {
		t.Helper()
		r, err := svc.SubmitExtra(ctx, taskID, ExtraRequestInput{Resources: []Ref{ref}})
		if err != nil {
			t.Fatalf("extra: %v", err)
		}
		if r.Granted {
			t.Fatalf("task %d request for %v should wait", taskID, ref)
		}
	}
	waitExtra(a.TaskID, Ref{Kind: "tool", Name: "R2"}) // T1->T2
	waitExtra(b.TaskID, Ref{Kind: "tool", Name: "R3"}) // T2->T3

	// T3 -> R1 closes the ring T1->T2->T3->T1.
	_, err := svc.SubmitExtra(ctx, c.TaskID, ExtraRequestInput{
		Resources: mkRefs("tool", "R1"),
	})
	if err == nil || asCoreErr(t, err).Kind != ErrCycle {
		t.Fatalf("expected cycle rejection, got err=%v", err)
	}
}

// TestTimeoutUncertainKeepsResources: timed-out resources are not released
// to the next task until stop is confirmed.
func TestTimeoutUncertainKeepsResources(t *testing.T) {
	svc, ctx := testService(t)
	mustRegister(t, ctx, svc, mkRefs("tool", "drill")...)

	a := mkTask(t, ctx, svc, "slow", 100, 0, 250, mkRefs("tool", "drill"))
	b := mkTask(t, ctx, svc, "next", 100, 0, 60000, mkRefs("tool", "drill"))
	if !a.Granted {
		t.Fatal("A should start")
	}
	if b.Granted {
		t.Fatal("B should wait behind A")
	}

	time.Sleep(320 * time.Millisecond)
	swept, err := svc.SweepOnce(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if len(swept) != 1 || swept[0] != a.TaskID {
		t.Fatalf("sweep should mark only A uncertain, got %v", swept)
	}

	// A is uncertain but STILL holds drill: B must not be granted.
	av, err := svc.GetTask(ctx, a.TaskID)
	if err != nil {
		t.Fatal(err)
	}
	if av.State != "uncertain" || len(av.HeldResources) != 1 {
		t.Fatalf("A must be uncertain and still hold drill, got %+v", av)
	}
	if err := svc.GrantWave(ctx); err != nil {
		t.Fatal(err)
	}
	bv, err := svc.GetTask(ctx, b.TaskID)
	if err != nil {
		t.Fatal(err)
	}
	if bv.State != "waiting" {
		t.Fatalf("B must STILL wait while A is uncertain, got %s", bv.State)
	}

	// Late completion arriving after the timeout is honoured: only now are
	// the resources confirmed stopped and handed to B.
	if _, err := svc.Complete(ctx, a.TaskID); err != nil {
		t.Fatalf("late completion: %v", err)
	}
	bv, err = svc.GetTask(ctx, b.TaskID)
	if err != nil {
		t.Fatal(err)
	}
	if bv.State != "running" || len(bv.HeldResources) != 1 {
		t.Fatalf("B should run only after A's confirmed stop, got %+v", bv)
	}
}

// TestLateHeartbeatRecoversUncertain: an uncertain task that makes contact
// again goes back to running and keeps its resources.
func TestLateHeartbeatRecoversUncertain(t *testing.T) {
	svc, ctx := testService(t)
	mustRegister(t, ctx, svc, mkRefs("station", "cell")...)
	a := mkTask(t, ctx, svc, "flaky", 100, 0, 200, mkRefs("station", "cell"))

	time.Sleep(270 * time.Millisecond)
	if _, err := svc.SweepOnce(ctx); err != nil {
		t.Fatal(err)
	}
	av, err := svc.GetTask(ctx, a.TaskID)
	if err != nil {
		t.Fatal(err)
	}
	if av.State != "uncertain" {
		t.Fatalf("expected uncertain, got %s", av.State)
	}

	hb, err := svc.Heartbeat(ctx, a.TaskID, HeartbeatInput{})
	if err != nil {
		t.Fatal(err)
	}
	if hb.State != "running" || hb.Deadline == nil || len(hb.HeldResources) != 1 {
		t.Fatalf("heartbeat must restore running state with resource and new deadline, got %+v", hb)
	}
}

// TestRevokeOnlyConfirmedStopped: revocation is all-or-nothing per call and
// refuses resources the task does not hold.
func TestRevokeOnlyConfirmedStopped(t *testing.T) {
	svc, ctx := testService(t)
	mustRegister(t, ctx, svc,
		mkRefs("tool", "gripper", "tool", "camera", "station", "dock")...)

	a := mkTask(t, ctx, svc, "A", 100, 0, 60000,
		mkRefs("tool", "gripper", "tool", "camera"))
	b := mkTask(t, ctx, svc, "B", 100, 0, 60000, mkRefs("tool", "gripper"))
	_ = b

	// Refusing the whole revoke when even one item is not held by A.
	_, err := svc.Revoke(ctx, a.TaskID, RevokeInput{
		Resources: mkRefs("tool", "gripper", "tool", "camera", "tool", "absent"),
		Reason:    "mixed batch must fail atomically",
	})
	if err == nil || asCoreErr(t, err).Kind != ErrNotFound {
		t.Fatalf("unknown resource should fail, got %v", err)
	}

	// A different running task cannot revoke a resource it does not hold.
	other := mkTask(t, ctx, svc, "C", 100, 0, 60000, mkRefs("station", "dock"))
	if _, err := svc.Revoke(ctx, other.TaskID, RevokeInput{
		Resources: mkRefs("tool", "camera"),
		Reason:    "C does not hold camera",
	}); err == nil || asCoreErr(t, err).Kind != ErrNotHeld {
		t.Fatalf("revoke of non-held resource should be ErrNotHeld, got %v", err)
	}

	// Revoke just gripper (confirmed stopped): B is granted, A keeps camera.
	av, err := svc.Revoke(ctx, a.TaskID, RevokeInput{
		Resources: mkRefs("tool", "gripper"),
		Reason:    "gripper confirmed stopped",
	})
	if err != nil {
		t.Fatalf("revoke: %v", err)
	}
	if av.State != "running" || len(av.HeldResources) != 1 || av.HeldResources[0].Name != "camera" {
		t.Fatalf("A should still run holding only camera, got %+v", av)
	}
	bv, err := svc.GetTask(ctx, b.TaskID)
	if err != nil {
		t.Fatal(err)
	}
	if bv.State != "running" || bv.HeldResources[0].Name != "gripper" {
		t.Fatalf("B should now hold gripper, got %+v", bv)
	}
}

// TestPriorityAging: a long-waiting low-priority task overtakes a fresh
// high-priority task once its aged priority exceeds the newcomer.
func TestPriorityAging(t *testing.T) {
	svc, ctx := testService(t)
	mustRegister(t, ctx, svc,
		mkRefs("tool", "R1", "tool", "R2")...)

	// Holder occupies R1+R2.
	holder := mkTask(t, ctx, svc, "holder", 500, 0, 60000,
		mkRefs("tool", "R1", "tool", "R2"))

	// Low priority waiter L arrives first, aging 100/sec.
	low := mkTask(t, ctx, svc, "low", 10, 100, 60000, mkRefs("tool", "R1"))
	// Give it a moment of waiting time.
	time.Sleep(60 * time.Millisecond)
	// Fresh high-priority waiter H: base 110 > low base 10, but low has
	// already aged ~6 points; after ~1s low clearly overtakes.
	high := mkTask(t, ctx, svc, "high", 110, 0, 60000, mkRefs("tool", "R1"))

	lv, _ := svc.GetTask(ctx, low.TaskID)
	hv, _ := svc.GetTask(ctx, high.TaskID)
	if lv.QueuePosition != 2 || hv.QueuePosition != 1 {
		t.Fatalf("before aging kicks in high should lead — got low#%d high#%d eff low=%.1f high=%.1f",
			lv.QueuePosition, hv.QueuePosition, lv.EffectivePriority, hv.EffectivePriority)
	}

	// Wait until low's aging beats high's static 110.
	time.Sleep(1100 * time.Millisecond)
	lv, _ = svc.GetTask(ctx, low.TaskID)
	hv, _ = svc.GetTask(ctx, high.TaskID)
	if !(lv.EffectivePriority > hv.EffectivePriority) {
		t.Fatalf("aged low %.2f should overtake high %.2f", lv.EffectivePriority, hv.EffectivePriority)
	}
	if lv.QueuePosition != 1 {
		t.Fatalf("low must be head of queue, position=%d", lv.QueuePosition)
	}

	// Release the holder: LOW (aged) wins, not the fresh high-priority task.
	if _, err := svc.Complete(ctx, holder.TaskID); err != nil {
		t.Fatal(err)
	}
	lv, _ = svc.GetTask(ctx, low.TaskID)
	hv, _ = svc.GetTask(ctx, high.TaskID)
	if lv.State != "running" {
		t.Fatalf("aged low-priority task should acquire first, state=%s", lv.State)
	}
	if hv.State != "waiting" {
		t.Fatalf("high-priority newcomer should still wait, state=%s", hv.State)
	}
}

// TestRestartRecovery: allocation state survives a full pool restart and
// the wait graph is rebuilt; an uncertain task keeps its fences.
func TestRestartRecovery(t *testing.T) {
	dsn := testutil.IsolatedDSN(t)
	ctx := context.Background()

	pool1 := testutil.NewPool(t, dsn)
	st1 := &store.Store{Pool: pool1}
	if err := st1.Migrate(ctx); err != nil {
		t.Fatal(err)
	}
	key, _ := evidence.GenerateKey()
	raw, _ := evidence.ParseKey(key)
	svc1 := NewService(pool1, evidence.NewSigner(raw), 1.0)

	mustRegister(t, ctx, svc1, mkRefs("tool", "drill", "station", "bay")...)
	a := mkTask(t, ctx, svc1, "persisted-A", 100, 0, 200,
		mkRefs("tool", "drill", "station", "bay"))
	b := mkTask(t, ctx, svc1, "persisted-B", 100, 0, 60000, mkRefs("tool", "drill"))
	time.Sleep(260 * time.Millisecond)
	if _, err := svc1.SweepOnce(ctx); err != nil {
		t.Fatal(err)
	}
	pool1.Close()

	// Restart with a brand new pool and service instance.
	pool2 := testutil.NewPool(t, dsn)
	st2 := &store.Store{Pool: pool2}
	if err := st2.Migrate(ctx); err != nil {
		t.Fatal(err)
	}
	svc2 := NewService(pool2, evidence.NewSigner(raw), 1.0)
	stats, err := svc2.Recover(ctx)
	if err != nil {
		t.Fatalf("recover: %v", err)
	}
	if stats["uncertain"] != 1 || stats["waiting"] != 1 {
		t.Fatalf("recovery stats wrong: %v", stats)
	}

	av, err := svc2.GetTask(ctx, a.TaskID)
	if err != nil {
		t.Fatal(err)
	}
	if av.State != "uncertain" || len(av.HeldResources) != 2 {
		t.Fatalf("A must remain uncertain holding 2 resources after restart, got %+v", av)
	}
	g, err := svc2.WaitGraph(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if len(g.Edges) != 1 {
		t.Fatalf("rebuilt graph must have exactly one edge, got %+v", g.Edges)
	}
	edge := g.Edges[0]
	if edge.WaiterTaskID != b.TaskID || edge.HolderTaskID != a.TaskID {
		t.Fatalf("rebuilt edge must show B(%d)->A(%d), got %+v", b.TaskID, a.TaskID, edge)
	}

	// Grant wave after restart must still respect the uncertain fence.
	if err := svc2.GrantWave(ctx); err != nil {
		t.Fatal(err)
	}
	bv, err := svc2.GetTask(ctx, b.TaskID)
	if err != nil {
		t.Fatal(err)
	}
	if bv.State != "waiting" {
		t.Fatalf("waiter B must remain blocked across restart, state=%s", bv.State)
	}
}

// TestEvidenceSignatureAndTamper: every grant/release/timeout writes a
// signed record; the signature verifies and a tampered field breaks it.
func TestEvidenceSignatureAndTamper(t *testing.T) {
	svc, ctx := testService(t)
	mustRegister(t, ctx, svc, mkRefs("tool", "drill")...)
	a := mkTask(t, ctx, svc, "ev", 100, 0, 60000, mkRefs("tool", "drill"))
	if _, err := svc.Complete(ctx, a.TaskID); err != nil {
		t.Fatal(err)
	}
	evs, err := svc.ListEvidence(ctx, &a.TaskID, 50)
	if err != nil {
		t.Fatal(err)
	}
	var sawGrant, sawComplete bool
	for _, e := range evs {
		if !e.Valid {
			t.Fatalf("evidence %d fails signature verification: %s", e.ID, e.Canonical)
		}
		if e.Event == "granted" {
			sawGrant = true
		}
		if e.Event == "completed" {
			sawComplete = true
		}
	}
	if !sawGrant || !sawComplete {
		t.Fatalf("expected granted and completed evidence, got %+v", evs)
	}

	// Tamper with the signed canonical payload directly; HMAC verification
	// must detect it.
	_, err = svc.pool.Exec(ctx,
		`UPDATE audit_events SET canonical=replace(canonical,'event=granted','event=forged')
		 WHERE id = (SELECT id FROM audit_events WHERE event='granted' LIMIT 1)`)
	if err != nil {
		t.Fatal(err)
	}
	evs, err = svc.ListEvidence(ctx, &a.TaskID, 50)
	if err != nil {
		t.Fatal(err)
	}
	var tamperedDetected bool
	for _, e := range evs {
		if !e.Valid {
			tamperedDetected = true
		}
		if strings.Contains(e.Canonical, "event=forged") && e.Valid {
			t.Fatal("forged record must not validate")
		}
	}
	if !tamperedDetected {
		t.Fatal("tampered audit row must be detected by HMAC verification")
	}
}

// TestUnknownResourcesRejected guards the foreign key validation path.
func TestUnknownResourcesRejected(t *testing.T) {
	svc, ctx := testService(t)
	_, err := svc.CreateTask(ctx, CreateTaskInput{
		Label:     "ghost",
		Resources: mkRefs("tool", "ghost-tool"),
	})
	if err == nil || asCoreErr(t, err).Kind != ErrNotFound {
		t.Fatalf("unknown resource must be rejected, got %v", err)
	}
}

// TestFailReleasesEverything confirms confirmed failure frees the full set.
func TestFailReleasesEverything(t *testing.T) {
	svc, ctx := testService(t)
	mustRegister(t, ctx, svc, mkRefs("tool", "a", "station", "b")...)
	x := mkTask(t, ctx, svc, "X", 100, 0, 60000, mkRefs("tool", "a", "station", "b"))
	y := mkTask(t, ctx, svc, "Y", 100, 0, 60000, mkRefs("station", "b"))
	if _, err := svc.Fail(ctx, x.TaskID, "confirmed collision"); err != nil {
		t.Fatal(err)
	}
	yv, err := svc.GetTask(ctx, y.TaskID)
	if err != nil {
		t.Fatal(err)
	}
	if yv.State != "running" {
		t.Fatalf("Y should run after X's confirmed failure, got %s", yv.State)
	}
}

// TestDynamicRequestWhileWaitingRejected ensures extra requests are only
// accepted from running tasks.
func TestDynamicRequestWhileWaitingRejected(t *testing.T) {
	svc, ctx := testService(t)
	mustRegister(t, ctx, svc, mkRefs("tool", "a", "tool", "b")...)
	holder := mkTask(t, ctx, svc, "h", 100, 0, 60000, mkRefs("tool", "a"))
	waiter := mkTask(t, ctx, svc, "w", 100, 0, 60000, mkRefs("tool", "a"))
	_ = holder
	_, err := svc.SubmitExtra(ctx, waiter.TaskID, ExtraRequestInput{
		Resources: mkRefs("tool", "b"),
	})
	if err == nil || asCoreErr(t, err).Kind != ErrState {
		t.Fatalf("extra request on a waiting task must be ErrState, got %v", err)
	}
}

// TestEvidenceCanonicalDeterminism is a pure crypto sanity check for the
// canonicalization/signing split.
func TestEvidenceCanonicalDeterminism(t *testing.T) {
	tid, rid := int64(7), int64(9)
	at := time.Date(2026, 9, 23, 12, 0, 0, 0, time.UTC)
	e := evidence.Event{
		AuditID: 42, At: at, Event: "granted", TaskID: &tid, RequestID: &rid,
		Resources: []evidence.Resource{
			{Kind: "tool", Name: "Z"}, {Kind: "tool", Name: "A"},
		},
	}
	c1 := evidence.Canonical(e)
	c2 := evidence.Canonical(e)
	if c1 != c2 || !strings.Contains(c1, "resources=tool/A,tool/Z") {
		t.Fatalf("canonical payload must be deterministic and sorted, got %q", c1)
	}
	s := evidence.NewSigner([]byte("0123456789abcdef0123456789abcdef"))
	sig := s.Sign(c1)
	if !s.Verify(c1, sig) || s.Verify(c1+"tampered", sig) {
		t.Fatal("HMAC verify must accept authentic and reject tampered payloads")
	}
}
