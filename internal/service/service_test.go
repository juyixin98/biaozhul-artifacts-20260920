package service_test

import (
	"context"
	"errors"
	"fmt"
	"os"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"deadlockcheck/internal/service"
	"deadlockcheck/internal/store"
)

func dsn() string {
	if v := os.Getenv("DATABASE_DSN"); v != "" {
		return v
	}
	return "postgres://deadlock:deadlock_pw_068@localhost:5432/deadlock_db?sslmode=disable"
}

func newService(t *testing.T, cfg service.Config) (*service.Service, *store.Store) {
	t.Helper()
	ctx := context.Background()
	st, err := store.Open(ctx, dsn())
	if err != nil {
		t.Skipf("postgresql not available, skipping: %v", err)
	}
	t.Cleanup(st.Close)
	cleanDB(ctx, t, st.Pool)
	svc, err := service.New(ctx, st, cfg)
	if err != nil {
		t.Fatalf("service: %v", err)
	}
	return svc, st
}

func cleanDB(ctx context.Context, t *testing.T, pool *pgxpool.Pool) {
	t.Helper()
	for _, q := range []string{
		`TRUNCATE task_events, hold_ledger, task_resources, tasks, resources RESTART IDENTITY CASCADE`,
		`DELETE FROM meta WHERE key IN ('fence_epoch','evidence_seed','server_id')`,
	} {
		if _, err := pool.Exec(ctx, q); err != nil {
			t.Fatalf("cleanup %q: %v", q, err)
		}
	}
}

func regResources(t *testing.T, svc *service.Service, ids ...string) {
	t.Helper()
	ctx := context.Background()
	for _, id := range ids {
		kind := "tool"
		if len(id) > 0 && id[0] == 's' {
			kind = "station"
		}
		if _, err := svc.RegisterResource(ctx, service.RegisterResourceReq{ID: id, Kind: kind}); err != nil {
			t.Fatalf("register %s: %v", id, err)
		}
	}
}

func mustCreate(t *testing.T, svc *service.Service, req service.CreateTaskReq) *service.AcquireResult {
	t.Helper()
	res, err := svc.CreateTask(context.Background(), req)
	if err != nil {
		t.Fatalf("create %s: %v", req.ID, err)
	}
	return res
}

func state(t *testing.T, svc *service.Service, id string) string {
	t.Helper()
	d, err := svc.TaskDetailFor(context.Background(), id)
	if err != nil {
		t.Fatalf("detail %s: %v", id, err)
	}
	return d.Task.State
}

// TestAtomicAllOrNothing: a task blocked on one resource must hold ZERO
// resources — partial acquisition is never visible.
func TestAtomicAllOrNothing(t *testing.T) {
	svc, _ := newService(t, service.DefaultConfig())
	regResources(t, svc, "tool-x", "tool-y")

	r := mustCreate(t, svc, service.CreateTaskReq{
		ID: "A", Priority: 10, Resources: []string{"tool-x"}, DeadlineMs: 5000,
	})
	if r.Status != "granted" {
		t.Fatalf("A: want granted, got %s", r.Status)
	}

	// tool-y is free in the system, yet B must not grab it partially.
	r = mustCreate(t, svc, service.CreateTaskReq{
		ID: "B", Priority: 10, Resources: []string{"tool-x", "tool-y"}, DeadlineMs: 5000,
	})
	if r.Status != "waiting" {
		t.Fatalf("B: want waiting, got %s", r.Status)
	}
	// B must not hold tool-y even though tool-y was free at request time.
	for _, w := range r.WaitReasons {
		if w.ResourceID == "tool-y" {
			if w.BlockedByTask != "" {
				t.Fatalf("tool-y should show free, blocked by %q", w.BlockedByTask)
			}
		}
		if w.ResourceID == "tool-x" && w.BlockedByTask != "A" {
			t.Fatalf("tool-x blocked by %q, want A", w.BlockedByTask)
		}
	}
	heldByB, err := svc.HeldCount(context.Background(), "B")
	if err != nil {
		t.Fatal(err)
	}
	if heldByB != 0 {
		t.Fatalf("B holds %d resources while waiting, want 0 (partial allocation!)", heldByB)
	}
}

// TestCrossResourceRequests exercises two crossing groups and proves the
// second waiter stays blocked with a precise wait reason until release.
func TestCrossResourceRequests(t *testing.T) {
	svc, _ := newService(t, service.DefaultConfig())
	regResources(t, svc, "tool-arm", "tool-welder", "station-a", "station-b")

	a := mustCreate(t, svc, service.CreateTaskReq{
		ID: "A", Priority: 100, Resources: []string{"tool-arm", "station-a"}, DeadlineMs: 5000,
	})
	if a.Status != "granted" || len(a.Holds) != 2 {
		t.Fatalf("A granted=%v holds=%d", a.Status == "granted", len(a.Holds))
	}
	b := mustCreate(t, svc, service.CreateTaskReq{
		ID: "B", Priority: 100, Resources: []string{"tool-arm", "station-b"}, DeadlineMs: 5000,
	})
	if b.Status != "waiting" {
		t.Fatalf("B want waiting got %s", b.Status)
	}

	// Evidence: every grant carries a valid signed token.
	for _, h := range a.Holds {
		p, err := svc.VerifyToken(h.Token)
		if err != nil {
			t.Fatalf("evidence token invalid: %v", err)
		}
		if p.TaskID != "A" || p.ResourceID != h.ResourceID {
			t.Fatalf("token payload mismatch: %+v", p)
		}
	}

	// Tampering with a token must be detected (real cryptographic verification).
	bad := a.Holds[0].Token
	if len(bad) > 3 {
		bad = bad[:len(bad)-2] + "AA"
		if _, err := svc.VerifyToken(bad); err == nil {
			t.Fatal("tampered evidence token verified successfully")
		}
	}

	// Completing A atomically releases both and promotes B to both.
	if _, err := svc.Complete(context.Background(), "A", a.Task.FenceEpoch); err != nil {
		t.Fatal(err)
	}
	if got := state(t, svc, "B"); got != service.StateRunning {
		t.Fatalf("B after A completes: %s, want running", got)
	}
	d, _ := svc.TaskDetailFor(context.Background(), "B")
	if len(d.Holds) != 2 {
		t.Fatalf("B holds %d after promotion, want 2", len(d.Holds))
	}
}

// TestDynamicCycleRejected: A holds x wants y, B holds y then wants x.
// The dynamic edge from B must be rejected because it closes a cycle.
func TestDynamicCycleRejected(t *testing.T) {
	svc, _ := newService(t, service.DefaultConfig())
	regResources(t, svc, "tool-x", "tool-y")

	a := mustCreate(t, svc, service.CreateTaskReq{
		ID: "A", Priority: 100, Resources: []string{"tool-x"}, DeadlineMs: 5000,
	})
	b := mustCreate(t, svc, service.CreateTaskReq{
		ID: "B", Priority: 100, Resources: []string{"tool-y"}, DeadlineMs: 5000,
	})
	if a.Status != "granted" || b.Status != "granted" {
		t.Fatal("setup grants failed")
	}

	// A wants y held by B: allowed, A waits — edge A -> B.
	ar, err := svc.AddResources(context.Background(),
		service.AddResourcesReq{TaskID: "A", Resources: []string{"tool-y"}})
	if err != nil {
		t.Fatal(err)
	}
	if ar.Status != "waiting" {
		t.Fatalf("A add y: want waiting, got %s", ar.Status)
	}

	// B wants x held by A: would make B -> A and close A<->B cycle.
	br, err := svc.AddResources(context.Background(),
		service.AddResourcesReq{TaskID: "B", Resources: []string{"tool-x"}})
	if err != nil {
		t.Fatal(err)
	}
	if br.Status != "rejected" || len(br.Cycles) == 0 {
		t.Fatalf("B add x: want rejected with cycles, got status=%s cycles=%d",
			br.Status, len(br.Cycles))
	}
	found := false
	for _, c := range br.Cycles {
		if len(c.Tasks) >= 2 {
			found = true
		}
	}
	if !found {
		t.Fatalf("cycle report lacks task path: %+v", br.Cycles)
	}
	// B keeps running with its original hold; no wanted row leaked.
	if got := state(t, svc, "B"); got != service.StateRunning {
		t.Fatalf("B state after cycle rejection: %s", got)
	}
	heldByB, err := svc.HeldCount(context.Background(), "B")
	if err != nil {
		t.Fatal(err)
	}
	if heldByB != 1 {
		t.Fatalf("B holds %d after rejected dynamic request, want 1", heldByB)
	}

	// Live graph view exposes edge and (currently) no cycle since B->A rejected.
	g, err := svc.Graph(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(g.Edges) != 1 || g.Edges[0].WaiterTask != "A" || g.Edges[0].HolderTask != "B" {
		t.Fatalf("graph edges wrong: %+v", g.Edges)
	}
}

// TestTimeoutUncertainAndLateCompletion: timed-out holder keeps its
// resources fenced. A waiter must NOT receive them, then a late completion
// is honoured and finally unblocks the waiter.
func TestTimeoutUncertainAndLateCompletion(t *testing.T) {
	cfg := service.DefaultConfig()
	svc, _ := newService(t, cfg)
	regResources(t, svc, "tool-r")

	// 250ms lease, no heartbeats.
	a := mustCreate(t, svc, service.CreateTaskReq{
		ID: "A", Priority: 100, Resources: []string{"tool-r"}, DeadlineMs: 200,
	})
	if a.Status != "granted" {
		t.Fatal("A not granted")
	}
	epoch := a.Task.FenceEpoch
	b := mustCreate(t, svc, service.CreateTaskReq{
		ID: "B", Priority: 100, Resources: []string{"tool-r"}, DeadlineMs: 5000,
	})
	if b.Status != "waiting" {
		t.Fatal("B should wait")
	}

	time.Sleep(700 * time.Millisecond)
	timedOut, err := svc.SweepTimeouts(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(timedOut) != 1 || timedOut[0] != "A" {
		t.Fatalf("sweep = %v, want [A]", timedOut)
	}
	if got := state(t, svc, "A"); got != service.StateUncertain {
		t.Fatalf("A state %s, want uncertain", got)
	}

	// Crucial invariant: B still does NOT get tool-r.
	if got := state(t, svc, "B"); got != service.StateWaiting {
		t.Fatalf("B state while A uncertain: %s, want waiting (resource must not be reassigned)", got)
	}
	wr, err := svc.WaitReasons(context.Background(), "B")
	if err != nil {
		t.Fatal(err)
	}
	if len(wr) != 1 || wr[0].HolderState != service.StateUncertain {
		t.Fatalf("B wait reasons = %+v, want single uncertain-holder reason", wr)
	}

	// Another sweep must be a no-op (idempotent).
	if again, _ := svc.SweepTimeouts(context.Background()); len(again) != 0 {
		t.Fatalf("second sweep moved tasks again: %v", again)
	}

	// Heartbeats from the (possibly alive) worker are now refused.
	if _, err := svc.Heartbeat(context.Background(), "A", epoch); err == nil {
		t.Fatal("heartbeat accepted after timeout fencing")
	}

	// Late completion arrives with the ORIGINAL epoch: accepted, releases.
	if _, err := svc.Complete(context.Background(), "A", epoch); err != nil {
		t.Fatalf("late completion rejected: %v", err)
	}
	if got := state(t, svc, "A"); got != service.StateCompleted {
		t.Fatalf("A state %s, want completed", got)
	}
	if got := state(t, svc, "B"); got != service.StateRunning {
		t.Fatalf("B after late complete: %s, want running", got)
	}
}

// TestUncertainThenConfirmStop: if the late completion never comes, the
// operator's stop confirmation is what releases the fenced resources.
func TestUncertainThenConfirmStop(t *testing.T) {
	svc, _ := newService(t, service.DefaultConfig())
	regResources(t, svc, "tool-r")
	mustCreate(t, svc, service.CreateTaskReq{
		ID: "A", Priority: 100, Resources: []string{"tool-r"}, DeadlineMs: 150,
	})
	mustCreate(t, svc, service.CreateTaskReq{
		ID: "B", Priority: 100, Resources: []string{"tool-r"}, DeadlineMs: 5000,
	})
	time.Sleep(700 * time.Millisecond)
	if _, err := svc.SweepTimeouts(context.Background()); err != nil {
		t.Fatal(err)
	}
	if _, err := svc.ConfirmStop(context.Background(), "B"); err == nil {
		t.Fatal("confirm-stop on a waiting task must fail")
	}
	if _, err := svc.ConfirmStop(context.Background(), "A"); err != nil {
		t.Fatalf("confirm stop A: %v", err)
	}
	if got := state(t, svc, "A"); got != service.StateRevoked {
		t.Fatalf("A %s, want revoked", got)
	}
	if got := state(t, svc, "B"); got != service.StateRunning {
		t.Fatalf("B %s after confirmed stop, want running", got)
	}
}

// TestRevokeHandshake: revoking a running task releases nothing until
// stop is explicitly confirmed.
func TestRevokeHandshake(t *testing.T) {
	svc, _ := newService(t, service.DefaultConfig())
	regResources(t, svc, "tool-r")
	mustCreate(t, svc, service.CreateTaskReq{
		ID: "A", Priority: 100, Resources: []string{"tool-r"}, DeadlineMs: 60000,
	})
	mustCreate(t, svc, service.CreateTaskReq{
		ID: "B", Priority: 100, Resources: []string{"tool-r"}, DeadlineMs: 60000,
	})

	if _, err := svc.Revoke(context.Background(), "A"); err != nil {
		t.Fatal(err)
	}
	if got := state(t, svc, "A"); got != service.StateRevoking {
		t.Fatalf("A %s, want revoking", got)
	}
	// Still holding: B remains blocked.
	if got := state(t, svc, "B"); got != service.StateWaiting {
		t.Fatalf("B %s after mere revoke request, want waiting", got)
	}
	heldByA, err := svc.HeldCount(context.Background(), "A")
	if err != nil {
		t.Fatal(err)
	}
	if heldByA != 1 {
		t.Fatalf("A released %d resources before stop confirmation", 1-heldByA)
	}
	if _, err := svc.ConfirmStop(context.Background(), "A"); err != nil {
		t.Fatal(err)
	}
	if got := state(t, svc, "B"); got != service.StateRunning {
		t.Fatalf("B %s after confirmed stop, want running", got)
	}
}

// TestRevokeRejectsWorkerComplete: while revocation is pending the worker's
// own completion cannot release resources; only confirmed stop does.
func TestRevokeRejectsWorkerComplete(t *testing.T) {
	svc, _ := newService(t, service.DefaultConfig())
	regResources(t, svc, "tool-r")
	a := mustCreate(t, svc, service.CreateTaskReq{
		ID: "A", Priority: 100, Resources: []string{"tool-r"}, DeadlineMs: 60000,
	})
	mustCreate(t, svc, service.CreateTaskReq{
		ID: "B", Priority: 100, Resources: []string{"tool-r"}, DeadlineMs: 60000,
	})
	if _, err := svc.Revoke(context.Background(), "A"); err != nil {
		t.Fatal(err)
	}
	if _, err := svc.Complete(context.Background(), "A", a.Task.FenceEpoch); err == nil {
		t.Fatal("worker complete accepted while revocation pending")
	}
	if got := state(t, svc, "A"); got != service.StateRevoking {
		t.Fatalf("A %s, want revoking (still holding)", got)
	}
	if got := state(t, svc, "B"); got != service.StateWaiting {
		t.Fatalf("B %s, want waiting", got)
	}
}

// TestRevokeWaitingTaskHoldsNothing: a waiting-task revoke frees the queue
// immediately because it never held anything.
func TestRevokeWaitingTaskHoldsNothing(t *testing.T) {
	svc, _ := newService(t, service.DefaultConfig())
	regResources(t, svc, "tool-r")
	mustCreate(t, svc, service.CreateTaskReq{ID: "H", Resources: []string{"tool-r"}, DeadlineMs: 60000})
	mustCreate(t, svc, service.CreateTaskReq{ID: "W", Resources: []string{"tool-r"}, DeadlineMs: 60000})
	if _, err := svc.Revoke(context.Background(), "W"); err != nil {
		t.Fatal(err)
	}
	if got := state(t, svc, "W"); got != service.StateRevoked {
		t.Fatalf("W %s want revoked", got)
	}
	wr, _ := svc.WaitReasons(context.Background(), "H")
	if len(wr) != 0 {
		t.Fatalf("revoked waiter still appears in graph: %+v", wr)
	}
}

// TestPriorityAgingHeadStart: waiter OLD (prio 100) queues 60s before
// waiter NEW (prio 50). Both then age; when the holder frees at t=70s,
// OLD's effective priority is 30 vs NEW's 50 — aging promotes OLD first.
func TestPriorityAgingHeadStart(t *testing.T) {
	cfg := service.Config{
		AgingStep: time.Second, AgingBonusPerStep: 1, AgingCap: 1000,
		DefaultDeadline: time.Minute,
	}
	svc, _ := newService(t, cfg)
	regResources(t, svc, "tool-r")

	base := time.Date(2026, 9, 23, 12, 0, 0, 0, time.UTC)
	cur := base
	svc.SetClock(func() time.Time { return cur })

	h := mustCreate(t, svc, service.CreateTaskReq{
		ID: "H", Priority: 1, Resources: []string{"tool-r"}, DeadlineMs: 600000,
	})
	old := mustCreate(t, svc, service.CreateTaskReq{
		ID: "OLD", Priority: 100, Resources: []string{"tool-r"}, DeadlineMs: 600000,
	})
	if old.Status != "waiting" {
		t.Fatal("OLD should be waiting")
	}
	cur = base.Add(60 * time.Second)
	newer := mustCreate(t, svc, service.CreateTaskReq{
		ID: "NEW", Priority: 50, Resources: []string{"tool-r"}, DeadlineMs: 600000,
	})
	if newer.Status != "waiting" {
		t.Fatal("NEW should be waiting")
	}
	// Sanity check reported effective priorities via list.
	tasks, err := svc.ListTasks(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	scores := map[string]int{}
	for _, x := range tasks {
		scores[x.ID] = x.EffectivePriority
	}
	if scores["OLD"] >= scores["NEW"] {
		t.Fatalf("aging not reflected yet: OLD=%d NEW=%d", scores["OLD"], scores["NEW"])
	}

	// Free the resource: OLD must be promoted, NEW still waiting.
	if _, err := svc.Complete(context.Background(), "H", h.Task.FenceEpoch); err != nil {
		t.Fatal(err)
	}
	if got := state(t, svc, "OLD"); got != service.StateRunning {
		t.Fatalf("OLD %s, want running (aged priority should win)", got)
	}
	if got := state(t, svc, "NEW"); got != service.StateWaiting {
		t.Fatalf("NEW %s, want still waiting", got)
	}
}

// TestRestartRecovery simulates a process restart: epoch bumps, running
// holders become uncertain (resources stay fenced), waiters survive, and a
// stale worker from the previous epoch is rejected.
func TestRestartRecovery(t *testing.T) {
	svc, st := newService(t, service.DefaultConfig())
	regResources(t, svc, "tool-r", "tool-q")

	a := mustCreate(t, svc, service.CreateTaskReq{
		ID: "A", Priority: 100, Resources: []string{"tool-r"}, DeadlineMs: 60000,
	})
	mustCreate(t, svc, service.CreateTaskReq{
		ID: "W", Priority: 100, Resources: []string{"tool-r"}, DeadlineMs: 60000,
	})
	epochBefore := a.Task.FenceEpoch
	if epochBefore < 1 {
		t.Fatalf("epoch before restart = %d, want >=1", epochBefore)
	}

	// ---- simulate new process boot ----
	ctx := context.Background()
	svc2, err := service.New(ctx, st, service.DefaultConfig())
	if err != nil {
		t.Fatal(err)
	}
	rep, err := svc2.Recover(ctx)
	if err != nil {
		t.Fatalf("recover: %v", err)
	}
	if rep.FenceEpoch <= epochBefore {
		t.Fatalf("epoch not bumped: %d -> %d", epochBefore, rep.FenceEpoch)
	}
	if len(rep.ToUncertain) != 1 || rep.ToUncertain[0] != "A" {
		t.Fatalf("recovery uncertain list %v, want [A]", rep.ToUncertain)
	}
	if len(rep.StillWaiting) != 1 || rep.StillWaiting[0] != "W" {
		t.Fatalf("recovery waiting list %v, want [W]", rep.StillWaiting)
	}

	// A's resources remain held/fenced: W is still waiting.
	if got := state(t, svc2, "W"); got != service.StateWaiting {
		t.Fatalf("W after restart: %s, want waiting", got)
	}

	// Stale worker reports complete with the OLD epoch -> rejected.
	if _, err := svc2.Complete(ctx, "A", epochBefore); err == nil {
		t.Fatal("stale worker from previous epoch was accepted")
	} else {
		var rej *service.ErrRejected
		if !errors.As(err, &rej) {
			t.Fatalf("want rejection, got %v", err)
		}
	}
	if got := state(t, svc2, "A"); got != service.StateUncertain {
		t.Fatalf("A must stay uncertain after stale report: %s", got)
	}

	// Operator confirms the physical robot stopped; resource frees to W.
	if _, err := svc2.ConfirmStop(ctx, "A"); err != nil {
		t.Fatal(err)
	}
	if got := state(t, svc2, "W"); got != service.StateRunning {
		t.Fatalf("W after confirmed stop: %s, want running", got)
	}
}

// TestConcurrentAcquires: many tasks racing for one resource, exactly one
// must win and no holder-invariant violations occur.
func TestConcurrentAcquires(t *testing.T) {
	svc, st := newService(t, service.DefaultConfig())
	regResources(t, svc, "tool-only")

	const n = 20
	var wg sync.WaitGroup
	results := make(chan string, n)
	errs := make(chan error, n)
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			id := fmt.Sprintf("T%02d", i)
			r, err := svc.CreateTask(context.Background(), service.CreateTaskReq{
				ID: id, Priority: 100, Resources: []string{"tool-only"}, DeadlineMs: 60000,
			})
			if err != nil {
				errs <- err
				return
			}
			results <- r.Status
		}(i)
	}
	wg.Wait()
	close(results)
	close(errs)
	for err := range errs {
		t.Fatalf("concurrent create error: %v", err)
	}
	granted, waiting := 0, 0
	for s := range results {
		switch s {
		case "granted":
			granted++
		case "waiting":
			waiting++
		default:
			t.Fatalf("unexpected status %s", s)
		}
	}
	if granted != 1 || waiting != n-1 {
		t.Fatalf("granted=%d waiting=%d, want 1/%d", granted, waiting, n-1)
	}

	// Database-level uniqueness must agree: exactly one held row.
	var holders int
	if err := st.Pool.QueryRow(context.Background(),
		`SELECT count(*) FROM task_resources WHERE resource_id='tool-only' AND status='held'`,
	).Scan(&holders); err != nil {
		t.Fatal(err)
	}
	if holders != 1 {
		t.Fatalf("held rows = %d, want exactly 1", holders)
	}
}

// TestLedgerEvidenceChain checks the append-only grant/release trail.
func TestLedgerEvidenceChain(t *testing.T) {
	svc, st := newService(t, service.DefaultConfig())
	regResources(t, svc, "tool-r")
	a := mustCreate(t, svc, service.CreateTaskReq{
		ID: "A", Resources: []string{"tool-r"}, DeadlineMs: 60000,
	})
	if _, err := svc.Complete(context.Background(), "A", a.Task.FenceEpoch); err != nil {
		t.Fatal(err)
	}
	var actions []string
	rows, err := st.Pool.Query(context.Background(),
		`SELECT action FROM hold_ledger WHERE resource_id='tool-r' ORDER BY id`)
	if err != nil {
		t.Fatal(err)
	}
	for rows.Next() {
		var a string
		if err := rows.Scan(&a); err != nil {
			t.Fatal(err)
		}
		actions = append(actions, a)
	}
	rows.Close()
	want := []string{"wanted", "granted", "released"}
	if len(actions) != len(want) {
		t.Fatalf("ledger %v, want %v", actions, want)
	}
	for i := range want {
		if actions[i] != want[i] {
			t.Fatalf("ledger %v, want %v", actions, want)
		}
	}
}
