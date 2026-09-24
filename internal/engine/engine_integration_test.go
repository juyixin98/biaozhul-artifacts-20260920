// Package engine_test contains end-to-end integration tests that exercise the
// tick algorithm against a real PostgreSQL instance (no mocks of the
// database or clock).
//
// The target database is taken from BT_TEST_DATABASE_URL, defaulting to
// postgres://bt065b:bt065b@localhost:5432/btree065b?sslmode=disable. Each test
// creates its own isolated schema (search_path), so tests are independent and
// can run in parallel; schemas are dropped at the end of each test.
package engine_test

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"os"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"bt/internal/engine"
	"bt/internal/model"
	"bt/internal/store"
	"bt/internal/stub"
)

const testDBURL = "postgres://bt065b:bt065b@localhost:5432/btree065b?sslmode=disable"

// newIsolatedPool opens the admin database, creates a unique schema for the
// test, and returns a pool whose search_path pins every table to that schema.
func newIsolatedPool(t *testing.T) *pgxpool.Pool {
	t.Helper()
	base := os.Getenv("BT_TEST_DATABASE_URL")
	if base == "" {
		base = testDBURL
	}
	adminPool, err := pgxpool.New(context.Background(), base)
	if err != nil {
		t.Fatalf("connect test db: %v", err)
	}
	if err := adminPool.Ping(context.Background()); err != nil {
		adminPool.Close()
		t.Skipf("test database unavailable (%v); set BT_TEST_DATABASE_URL", err)
	}
	schema := "t_" + strings.ReplaceAll(t.Name(), "/", "_")
	schema = strings.Map(func(r rune) rune {
		if r >= 'a' && r <= 'z' || r >= '0' && r <= '9' || r == '_' {
			return r
		}
		return '_'
	}, schema)
	if _, err := adminPool.Exec(context.Background(), "DROP SCHEMA IF EXISTS "+schema+" CASCADE"); err != nil {
		adminPool.Close()
		t.Fatalf("drop schema: %v", err)
	}
	if _, err := adminPool.Exec(context.Background(), "CREATE SCHEMA "+schema); err != nil {
		adminPool.Close()
		t.Fatalf("create schema: %v", err)
	}
	adminPool.Close()

	u, err := url.Parse(base)
	if err != nil {
		t.Fatalf("parse db url: %v", err)
	}
	q := u.Query()
	if existing := q.Get("search_path"); existing != "" {
		q.Set("search_path", schema+","+existing)
	} else {
		q.Set("search_path", schema)
	}
	u.RawQuery = q.Encode()
	pool, err := pgxpool.New(context.Background(), u.String())
	if err != nil {
		t.Fatalf("isolated pool: %v", err)
	}
	t.Cleanup(func() {
		pool.Close()
		// Reconnect to drop the schema.
		cleanup, err := pgxpool.New(context.Background(), base)
		if err == nil {
			_, _ = cleanup.Exec(context.Background(), "DROP SCHEMA IF EXISTS "+schema+" CASCADE")
			cleanup.Close()
		}
	})
	return pool
}

type harness struct {
	eng  *engine.Engine
	st   *store.Store
	rg   *stub.Registry
	pool *pgxpool.Pool
	now  *fakeClock
}

type fakeClock struct{ t atomic.Int64 }

func newFakeClock(start time.Time) *fakeClock {
	f := &fakeClock{}
	f.t.Store(start.UnixNano())
	return f
}
func (f *fakeClock) Now() time.Time { return time.Unix(0, f.t.Load()) }
func (f *fakeClock) Advance(d time.Duration) {
	f.t.Add(int64(d))
}

func newHarness(t *testing.T, useFakeClock bool) *harness {
	t.Helper()
	pool := newIsolatedPool(t)
	st := store.New(pool)
	if err := st.Migrate(context.Background()); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	rg := stub.NewRegistry()
	opts := engine.Options{}
	var fc *fakeClock
	if useFakeClock {
		fc = newFakeClock(time.Date(2026, 1, 1, 12, 0, 0, 0, time.UTC))
		opts.Now = fc.Now
	}
	eng := engine.New(st, rg, opts)
	if err := eng.ReapOrphans(context.Background()); err != nil {
		t.Fatalf("reap: %v", err)
	}
	return &harness{eng: eng, st: st, rg: rg, pool: pool, now: fc}
}

func mustPublish(t *testing.T, h *harness, tree *model.Tree) *engine.PublishResult {
	t.Helper()
	res, err := h.eng.PublishTree(context.Background(), tree)
	if err != nil {
		t.Fatalf("publish: %v", err)
	}
	return res
}

func mustStart(t *testing.T, h *harness, treeID string, version int) string {
	t.Helper()
	execID, _, err := h.eng.StartExecution(context.Background(), treeID, version)
	if err != nil {
		t.Fatalf("start: %v", err)
	}
	return execID
}

func tick(t *testing.T, h *harness, execID string) *engine.TickResult {
	t.Helper()
	res, err := h.eng.Tick(context.Background(), execID)
	if err != nil {
		t.Fatalf("tick: %v", err)
	}
	return res
}

func rawJSON(v any) *json.RawMessage {
	b, _ := json.Marshal(v)
	r := json.RawMessage(b)
	return &r
}

func action(id, name string, nonIdem bool, args any) *model.Node {
	n := &model.Node{ID: id, Kind: model.KindAction, Stub: name, NonIdempotent: nonIdem}
	if args != nil {
		n.Args = rawJSON(args)
	}
	return n
}

func seqNode(id string, children ...*model.Node) *model.Node {
	return &model.Node{ID: id, Kind: model.KindSequence, Children: children}
}
func fallbackNode(id string, children ...*model.Node) *model.Node {
	return &model.Node{ID: id, Kind: model.KindFallback, Children: children}
}
func parallelNode(id string, succ, fail int, children ...*model.Node) *model.Node {
	return &model.Node{ID: id, Kind: model.KindParallel, Success: succ, Failure: fail, Children: children}
}
func timeoutNode(id string, ms int, child *model.Node) *model.Node {
	return &model.Node{ID: id, Kind: model.KindTimeout, MS: ms, Child: child}
}
func root(children ...*model.Node) *model.Node {
	return &model.Node{ID: "root", Kind: model.KindRoot, Children: children}
}

func invocations(t *testing.T, h *harness, execID, nodeID string) int {
	t.Helper()
	n, err := h.eng.StubInvocationCount(context.Background(), execID, nodeID)
	if err != nil {
		t.Fatalf("invocations: %v", err)
	}
	return n
}

// --- tests -------------------------------------------------------------------

// 1. Sequence: success of every child yields tree success, left to right.
func TestSequenceAllSuccess(t *testing.T) {
	h := newHarness(t, false)
	tree := &model.Tree{Name: "seq-ok", Root: root(
		seqNode("seq",
			action("a1", "succeed", false, nil),
			action("a2", "succeed", false, map[string]any{"k": "v"}),
		),
	)}
	pub := mustPublish(t, h, tree)
	execID := mustStart(t, h, pub.TreeID, pub.Version)

	r := tick(t, h, execID)
	if r.TreeStatus != "success" || r.Status != "completed" {
		t.Fatalf("want completed/success, got %s/%s", r.Status, r.TreeStatus)
	}
	if r.Nodes["a1"] != "success" || r.Nodes["a2"] != "success" || r.Nodes["seq"] != "success" {
		t.Fatalf("unexpected nodes: %v", r.Nodes)
	}
}

// 2. Sequence stops at the first failure; later children never execute.
func TestSequenceShortCircuitsOnFailure(t *testing.T) {
	h := newHarness(t, false)
	tree := &model.Tree{Name: "seq-fail", Root: root(
		seqNode("seq",
			action("a1", "succeed", false, nil),
			action("a2", "fail", false, nil),
			action("a3", "succeed", false, nil),
		),
	)}
	pub := mustPublish(t, h, tree)
	execID := mustStart(t, h, pub.TreeID, pub.Version)
	r := tick(t, h, execID)
	if r.TreeStatus != "failure" {
		t.Fatalf("want failure, got %s", r.TreeStatus)
	}
	if r.Nodes["a3"] != "" {
		t.Fatalf("a3 must not execute, nodes=%v", r.Nodes)
	}
}

// 3. Non-idempotent success dedup across many ticks and a full restart:
// the physical stub is executed exactly once.
func TestNonIdempotentSuccessRunsOnceAcrossRestart(t *testing.T) {
	h := newHarness(t, false)
	tree := &model.Tree{Name: "dedup", Root: root(
		seqNode("seq",
			action("charge", "succeed", true, nil), // non-idempotent charge
			action("wait-b", "gate", false, map[string]any{"token": "g1"}),
		),
	)}
	pub := mustPublish(t, h, tree)
	execID := mustStart(t, h, pub.TreeID, pub.Version)

	// Tick 1: charge succeeds (physically once), gate starts, tree running.
	r := tick(t, h, execID)
	if r.TreeStatus != "running" || r.Calls["charge"].Status != "success" {
		t.Fatalf("want running with charge success, got %s calls=%v", r.TreeStatus, r.Calls)
	}
	if got := invocations(t, h, execID, "charge"); got != 1 {
		t.Fatalf("charge invocations after tick1 = %d, want 1", got)
	}

	// Simulate a full process restart: brand-new engine AND registry over
	// the same database. In-memory gate waiters of the dead process are
	// gone; the reaper converts the orphan 'running' gate call to
	// interrupted, so the next tick re-attaches the gate under a new attempt.
	h.rg = stub.NewRegistry()
	h.eng = engine.New(h.st, h.rg, engine.Options{})
	if err := h.eng.ReapOrphans(context.Background()); err != nil {
		t.Fatalf("reap after restart: %v", err)
	}
	r = tick(t, h, execID)
	if r.TreeStatus != "running" {
		t.Fatalf("still running expected, got %s", r.TreeStatus)
	}
	if got := invocations(t, h, execID, "charge"); got != 1 {
		t.Fatalf("non-idempotent charge re-executed after restart: count=%d", got)
	}
	if r.Calls["charge"].Status != "success" {
		t.Fatalf("charge latch lost: %v", r.Calls["charge"])
	}

	// Resolve the (re-attached) gate; the latch updates and the tree
	// advances on the following explicit tick.
	if ok := h.rg.ResolveGate("g1", stub.Result{Status: "success"}); !ok {
		t.Fatal("gate g1 not held after restart re-attach")
	}
	r = tick(t, h, execID)
	if r.TreeStatus != "success" {
		t.Fatalf("want success, got %s (%v)", r.TreeStatus, r.Nodes)
	}
	if got := invocations(t, h, execID, "charge"); got != 1 {
		t.Fatalf("charge executed %d times, want exactly 1", got)
	}
}

// 4. Parallel success threshold with a race: first success to reach the
// threshold decides success and still-running siblings are canceled.
func TestParallelSuccessThresholdCancelsSiblings(t *testing.T) {
	h := newHarness(t, false)
	tree := &model.Tree{Name: "par-race", Root: root(
		parallelNode("par", 1, 1, // success threshold = 1
			action("p1", "gate", false, map[string]any{"token": "p1"}),
			action("p2", "gate", false, map[string]any{"token": "p2"}),
		),
	)}
	pub := mustPublish(t, h, tree)
	execID := mustStart(t, h, pub.TreeID, pub.Version)

	r := tick(t, h, execID)
	if r.TreeStatus != "running" {
		t.Fatalf("want running, got %s", r.TreeStatus)
	}

	// p1 wins.
	if ok := h.rg.ResolveGate("p1", stub.Result{Status: "success"}); !ok {
		t.Fatal("gate p1 missing")
	}
	// Give the watcher a moment to persist the latch.
	waitForCallStatus(t, h, execID, "p1", "success")

	r = tick(t, h, execID)
	if r.TreeStatus != "success" {
		t.Fatalf("want success, got %s nodes=%v", r.TreeStatus, r.Nodes)
	}
	if r.Nodes["par"] != "success" {
		t.Fatalf("parallel not success: %v", r.Nodes)
	}
	// The losing sibling p2 must have been canceled (latch + node).
	if r.Calls["p2"].Status != "canceled" {
		t.Fatalf("losing sibling p2 should be canceled, call=%v", r.Calls["p2"])
	}
	if r.Nodes["p2"] != "canceled" {
		t.Fatalf("losing sibling node p2 should be canceled, nodes=%v", r.Nodes)
	}
	// A late resolution for the canceled gate must be rejected.
	if ok := h.rg.ResolveGate("p2", stub.Result{Status: "success"}); ok {
		t.Fatal("late gate resolution for canceled p2 was accepted")
	}
	// Even after that, the tree stays success.
	snap, err := h.eng.Snapshot(context.Background(), execID)
	if err != nil {
		t.Fatal(err)
	}
	if snap.Status != "success" {
		t.Fatalf("late result changed tree status: %s", snap.Status)
	}
}

// 5. Parallel failure threshold (3 children, need 2 successes OR 2 failures).
func TestParallelFailureThreshold(t *testing.T) {
	h := newHarness(t, false)
	tree := &model.Tree{Name: "par-fail", Root: root(
		parallelNode("par", 2, 2,
			action("p1", "gate", false, map[string]any{"token": "q1"}),
			action("p2", "gate", false, map[string]any{"token": "q2"}),
			action("p3", "gate", false, map[string]any{"token": "q3"}),
		),
	)}
	pub := mustPublish(t, h, tree)
	execID := mustStart(t, h, pub.TreeID, pub.Version)
	tick(t, h, execID)

	h.rg.ResolveGate("q1", stub.Result{Status: "failure", Err: "boom1"})
	waitForCallStatus(t, h, execID, "p1", "failure")
	r := tick(t, h, execID)
	if r.TreeStatus != "running" {
		t.Fatalf("one failure with threshold 2 keeps running, got %s", r.TreeStatus)
	}

	h.rg.ResolveGate("q2", stub.Result{Status: "failure", Err: "boom2"})
	waitForCallStatus(t, h, execID, "p2", "failure")
	r = tick(t, h, execID)
	if r.TreeStatus != "failure" {
		t.Fatalf("want failure after 2 failures, got %s", r.TreeStatus)
	}
	// The still-running third child is canceled when the parent decides.
	if r.Nodes["p3"] != "canceled" || r.Calls["p3"].Status != "canceled" {
		t.Fatalf("pending sibling p3 should be canceled: node=%s call=%v",
			r.Nodes["p3"], r.Calls["p3"])
	}
}

// 6. Timeout: a child that does not finish in time fails the timeout node,
// the running child is canceled, and a late result cannot resurrect it.
func TestTimeoutFiresAndLateResultRejected(t *testing.T) {
	h := newHarness(t, true)
	tree := &model.Tree{Name: "timeout", Root: root(
		timeoutNode("to", 100,
			action("slow", "gate", true, map[string]any{"token": "slow"}),
		),
	)}
	pub := mustPublish(t, h, tree)
	execID := mustStart(t, h, pub.TreeID, pub.Version)

	r := tick(t, h, execID)
	if r.TreeStatus != "running" {
		t.Fatalf("want running before deadline, got %s", r.TreeStatus)
	}

	// Advance the fake clock beyond the 100ms budget.
	h.now.Advance(150 * time.Millisecond)
	r = tick(t, h, execID)
	if r.TreeStatus != "failure" || r.Nodes["to"] != "failure" {
		t.Fatalf("timeout should fail, got %s nodes=%v", r.TreeStatus, r.Nodes)
	}
	if r.Calls["slow"].Status != "canceled" || r.Nodes["slow"] != "canceled" {
		t.Fatalf("timed-out child should be canceled: call=%v node=%s",
			r.Calls["slow"], r.Nodes["slow"])
	}

	// A late successful result from the canceled stub must not be accepted.
	if ok := h.rg.ResolveGate("slow", stub.Result{Status: "success"}); ok {
		t.Fatal("late result from timed-out action was accepted by gate")
	}
	snap, _ := h.eng.Snapshot(context.Background(), execID)
	if snap.Status != "failure" {
		t.Fatalf("late result resurrected tree: %s", snap.Status)
	}
}

// 7. Timeout success race: child succeeds just before the deadline; the
// timeout node succeeds and no failure is latched.
func TestTimeoutChildSucceedsInTime(t *testing.T) {
	h := newHarness(t, true)
	tree := &model.Tree{Name: "timeout-race", Root: root(
		timeoutNode("to", 100, action("fast", "gate", false, map[string]any{"token": "f"})),
	)}
	pub := mustPublish(t, h, tree)
	execID := mustStart(t, h, pub.TreeID, pub.Version)
	tick(t, h, execID)
	h.now.Advance(50 * time.Millisecond) // still within budget
	h.rg.ResolveGate("f", stub.Result{Status: "success"})
	waitForCallStatus(t, h, execID, "fast", "success")
	r := tick(t, h, execID)
	if r.TreeStatus != "success" || r.Nodes["to"] != "success" {
		t.Fatalf("in-time success should succeed timeout, got %s nodes=%v", r.TreeStatus, r.Nodes)
	}
}

// 8. Timeout deadline survives a process restart (persisted deadline_at).
func TestTimeoutDeadlinePersistsAcrossRestart(t *testing.T) {
	h := newHarness(t, true)
	tree := &model.Tree{Name: "timeout-restart", Root: root(
		timeoutNode("to", 100, action("slow", "gate", false, map[string]any{"token": "sr"})),
	)}
	pub := mustPublish(t, h, tree)
	execID := mustStart(t, h, pub.TreeID, pub.Version)
	tick(t, h, execID) // sets deadline at t0+100ms

	// Restart with a new engine sharing the persisted deadline and clock.
	h.rg = stub.NewRegistry()
	h.eng = engine.New(h.st, h.rg, engine.Options{Now: h.now.Now})
	if err := h.eng.ReapOrphans(context.Background()); err != nil {
		t.Fatal(err)
	}
	h.now.Advance(150 * time.Millisecond)
	r := tick(t, h, execID)
	if r.TreeStatus != "failure" {
		t.Fatalf("deadline should persist across restart, got %s", r.TreeStatus)
	}
}

// 9. Fallback: first branch fails, second branch succeeds; tree success.
func TestFallbackBranches(t *testing.T) {
	h := newHarness(t, false)
	tree := &model.Tree{Name: "fb", Root: root(
		fallbackNode("fb",
			action("primary", "fail", false, nil),
			action("backup", "succeed", false, nil),
		),
	)}
	pub := mustPublish(t, h, tree)
	execID := mustStart(t, h, pub.TreeID, pub.Version)
	r := tick(t, h, execID)
	if r.TreeStatus != "success" {
		t.Fatalf("fallback should recover, got %s", r.TreeStatus)
	}
	if r.Nodes["primary"] != "failure" || r.Nodes["backup"] != "success" {
		t.Fatalf("unexpected fallback nodes: %v", r.Nodes)
	}
}

// 10. Fallback across ticks with an async first branch: an early failure is
// retried only when it is still failing; once a branch succeeds it latches.
func TestFallbackAsyncBranch(t *testing.T) {
	h := newHarness(t, false)
	tree := &model.Tree{Name: "fb-async", Root: root(
		fallbackNode("fb",
			action("try-async", "gate", false, map[string]any{"token": "async1"}),
			action("backup", "succeed", false, nil),
		),
	)}
	pub := mustPublish(t, h, tree)
	execID := mustStart(t, h, pub.TreeID, pub.Version)
	r := tick(t, h, execID)
	if r.TreeStatus != "running" {
		t.Fatalf("want running, got %s", r.TreeStatus)
	}
	// First branch fails asynchronously; the fallback should take backup.
	h.rg.ResolveGate("async1", stub.Result{Status: "failure", Err: "nope"})
	waitForCallStatus(t, h, execID, "try-async", "failure")
	r = tick(t, h, execID)
	if r.TreeStatus != "success" || r.Nodes["backup"] != "success" {
		t.Fatalf("fallback should move to backup after async failure, got %s nodes=%v",
			r.TreeStatus, r.Nodes)
	}
}

// 11. Tick interruption: canceling the request mid-tick records an
// interrupted tick that still consumes a durable sequence; the in-flight
// synchronous action is interrupted (not trusted as success) and re-runs.
func TestTickInterruptionConsumesSeqAndReruns(t *testing.T) {
	h := newHarness(t, false)
	tree := &model.Tree{Name: "interrupt", Root: root(
		seqNode("seq",
			// block with a long release so the tick is parked here.
			action("parked", "block", false, map[string]any{"release_ms": 60000}),
			action("after", "succeed", false, nil),
		),
	)}
	pub := mustPublish(t, h, tree)
	execID := mustStart(t, h, pub.TreeID, pub.Version)

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan *engine.TickResult, 1)
	go func() {
		res, err := h.eng.Tick(ctx, execID)
		if err != nil {
			t.Errorf("interrupted tick returned error: %v", err)
			return
		}
		done <- res
	}()
	time.Sleep(150 * time.Millisecond) // let the tick park in block
	cancel()
	var r *engine.TickResult
	select {
	case r = <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("interrupted tick never returned")
	}
	if r.Status != "interrupted" || r.Seq != 1 {
		t.Fatalf("want interrupted seq 1, got %s seq %d", r.Status, r.Seq)
	}

	// Next interrupted tick consumes the next durable sequence number: the
	// parked node is relaunched (no trusted success yet) and we cancel this
	// tick too, proving sequence advances exactly once per tick even across
	// repeated interruptions.
	ctx2, cancel2 := context.WithCancel(context.Background())
	done2 := make(chan *engine.TickResult, 1)
	go func() {
		res, err := h.eng.Tick(ctx2, execID)
		if err != nil {
			t.Errorf("second interrupted tick returned error: %v", err)
			return
		}
		done2 <- res
	}()
	time.Sleep(100 * time.Millisecond)
	cancel2()
	var r2 *engine.TickResult
	select {
	case r2 = <-done2:
	case <-time.After(5 * time.Second):
		t.Fatal("second interrupted tick never returned")
	}
	if r2.Status != "interrupted" || r2.Seq != 2 {
		t.Fatalf("want interrupted seq 2, got %s seq %d", r2.Status, r2.Seq)
	}

	// Every interrupted tick produced at most one physical invocation; the
	// non-successful parked action is allowed to (re)run — but never latched
	// success.
	if got := invocations(t, h, execID, "parked"); got != 2 {
		t.Fatalf("parked invocations = %d, want exactly 2 (one per interrupted attempt)", got)
	}

	if _, err := h.eng.Abort(context.Background(), execID); err != nil {
		t.Fatalf("abort: %v", err)
	}
}

// 12. Durable sequence survives a restart and remains gapless+monotonic.
func TestTickSequenceDurableAcrossRestart(t *testing.T) {
	h := newHarness(t, false)
	tree := &model.Tree{Name: "seqnum", Root: root(
		seqNode("seq",
			action("g", "gate", false, map[string]any{"token": "n"}),
		),
	)}
	pub := mustPublish(t, h, tree)
	execID := mustStart(t, h, pub.TreeID, pub.Version)
	r1 := tick(t, h, execID)
	if r1.Seq != 1 {
		t.Fatalf("first seq=%d want 1", r1.Seq)
	}
	h.eng = engine.New(h.st, h.rg, engine.Options{})
	_ = h.eng.ReapOrphans(context.Background())
	r2 := tick(t, h, execID)
	if r2.Seq != 2 {
		t.Fatalf("after restart seq=%d want 2", r2.Seq)
	}
}

// 13. Late result fence at SQL level: a result from a stale attempt is
// dropped even when it physically arrives late, and cannot resume the tree.
func TestLateResultFromStaleAttemptFenced(t *testing.T) {
	h := newHarness(t, false)
	var counter int64
	// Misbehaving stub: ignores ctx cancellation and returns success far too
	// late — simulating an orphaned physical attempt after cancel.
	h.rg.RegisterAsync("rogue", func(ctx context.Context, args json.RawMessage) <-chan stub.Result {
		atomic.AddInt64(&counter, 1)
		out := make(chan stub.Result, 1)
		go func() {
			select {
			case <-ctx.Done():
				// Do NOT stop: deliver success anyway after a delay, like an
				// external system that did not honor cancellation.
				time.Sleep(300 * time.Millisecond)
				out <- stub.Result{Status: "success"}
			case <-time.After(300 * time.Millisecond):
				out <- stub.Result{Status: "success"}
			}
		}()
		return out
	})

	tree := &model.Tree{Name: "rogue", Root: root(
		parallelNode("par", 1, 1,
			action("r", "rogue", true, nil),
			action("winner", "gate", false, map[string]any{"token": "w"}),
		),
	)}
	pub := mustPublish(t, h, tree)
	execID := mustStart(t, h, pub.TreeID, pub.Version)
	tick(t, h, execID) // both running

	// winner succeeds first -> parallel decides; rogue is canceled.
	h.rg.ResolveGate("w", stub.Result{Status: "success"})
	waitForCallStatus(t, h, execID, "winner", "success")
	r := tick(t, h, execID)
	if r.TreeStatus != "success" {
		t.Fatalf("want success, got %s", r.TreeStatus)
	}
	if r.Calls["r"].Status != "canceled" {
		t.Fatalf("rogue should be canceled, got %v", r.Calls["r"])
	}

	// Wait long enough for the rogue's late success to (try to) land.
	time.Sleep(500 * time.Millisecond)
	snap, _ := h.eng.Snapshot(context.Background(), execID)
	if snap.Status != "success" {
		t.Fatalf("late rogue result resurrected/changed execution: %s", snap.Status)
	}
	if snap.Calls["r"].Status != "canceled" {
		t.Fatalf("fence failed: rogue call became %s", snap.Calls["r"].Status)
	}
}

// 14. Abort cancels still-running children and subsequent ticks are rejected.
func TestAbortCancelsChildren(t *testing.T) {
	h := newHarness(t, false)
	tree := &model.Tree{Name: "abort", Root: root(
		action("g", "gate", false, map[string]any{"token": "ab"}),
	)}
	pub := mustPublish(t, h, tree)
	execID := mustStart(t, h, pub.TreeID, pub.Version)
	tick(t, h, execID)
	snap, err := h.eng.Abort(context.Background(), execID)
	if err != nil {
		t.Fatalf("abort: %v", err)
	}
	if snap.Status != "aborted" || snap.Nodes["g"] != "canceled" || snap.Calls["g"].Status != "canceled" {
		t.Fatalf("bad abort snapshot: %+v", snap)
	}
	if _, err := h.eng.Tick(context.Background(), execID); !errors.Is(err, store.ErrAlreadyTerminal) {
		t.Fatalf("tick after abort should be ErrAlreadyTerminal, got %v", err)
	}
	// A late gate result is rejected.
	if h.rg.ResolveGate("ab", stub.Result{Status: "success"}) {
		t.Fatal("late result accepted after abort")
	}
}

// 15. Definitions are immutable and executions bind a specific version:
// publishing a changed definition yields a new version on the same lineage,
// existing executions keep running the old version.
func TestDefinitionVersioningAndBinding(t *testing.T) {
	h := newHarness(t, false)
	t1 := &model.Tree{Name: "svc", Root: root(action("a", "fail", false, nil))}
	p1 := mustPublish(t, h, t1)
	if p1.Version != 1 {
		t.Fatalf("first version=%d want 1", p1.Version)
	}
	// Republish identical content -> same hash, idempotent same version.
	p1b := mustPublish(t, h, t1)
	if p1b.Version != 1 || p1b.TreeID != p1.TreeID || p1b.Hash != p1.Hash {
		t.Fatalf("identical republish changed identity: %+v vs %+v", p1, p1b)
	}
	// Start an execution against v1.
	execV1 := mustStart(t, h, p1.TreeID, 1)
	tick(t, h, execV1)
	snap, _ := h.eng.Snapshot(context.Background(), execV1)
	if snap.TreeVersion != 1 || snap.Status != "failure" {
		t.Fatalf("v1 exec bound wrong: ver=%d status=%s", snap.TreeVersion, snap.Status)
	}

	// Publish changed definition -> same lineage id, version 2.
	t2 := &model.Tree{Name: "svc", Root: root(action("a", "succeed", false, nil))}
	p2 := mustPublish(t, h, t2)
	if p2.TreeID != p1.TreeID {
		t.Fatalf("lineage id changed: %s vs %s", p2.TreeID, p1.TreeID)
	}
	if p2.Version != 2 || p2.Hash == p1.Hash {
		t.Fatalf("changed def should be v2 with new hash: %+v", p2)
	}
	// A new execution defaults to latest (v2) and succeeds.
	execV2 := mustStart(t, h, p2.TreeID, 0)
	r := tick(t, h, execV2)
	if r.TreeStatus != "success" {
		t.Fatalf("v2 should succeed, got %s", r.TreeStatus)
	}
	snap2, _ := h.eng.Snapshot(context.Background(), execV2)
	if snap2.TreeVersion != 2 {
		t.Fatalf("new exec not bound to v2: %d", snap2.TreeVersion)
	}
}

// 16. Invalid definitions are rejected at publish time (stable, unique ids).
func TestValidationRejectsDuplicateIDs(t *testing.T) {
	h := newHarness(t, false)
	bad := &model.Tree{Name: "bad", Root: root(
		seqNode("dup", action("dup", "succeed", false, nil)),
	)}
	if _, err := h.eng.PublishTree(context.Background(), bad); err == nil {
		t.Fatal("expected duplicate node id rejection")
	}
}

// waitForCallStatus polls the persisted latch until it reaches the wanted
// status (async watchers commit on their own goroutine).
func waitForCallStatus(t *testing.T, h *harness, execID, nodeID, want string) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		snap, err := h.eng.Snapshot(context.Background(), execID)
		if err != nil {
			t.Fatalf("snapshot: %v", err)
		}
		if c, ok := snap.Calls[nodeID]; ok && c.Status == want {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	c, _ := h.eng.Snapshot(context.Background(), execID)
	t.Fatalf("call %s never reached %s; snapshot=%s", nodeID, want, fmt.Sprintf("%+v", c))
}
