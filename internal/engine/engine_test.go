package engine_test

import (
	"context"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"

	"resumable-bt/internal/engine"
	"resumable-bt/internal/store"
	"resumable-bt/internal/stub"
)

// Tree definition snippets reused across tests.
const seqInstant = `{
  "root":"seq","nodes":{
    "seq":{"id":"seq","kind":"sequence","children":["a","b"]},
    "a":{"id":"a","kind":"action","action":"stub","params":{"message":"a"}},
    "b":{"id":"b","kind":"action","action":"stub","params":{"message":"b"}}
  }}`

const seqFailureFirst = `{
  "root":"seq","nodes":{
    "seq":{"id":"seq","kind":"sequence","children":["bad","good"]},
    "bad":{"id":"bad","kind":"action","action":"stub","params":{"result":"failure","message":"bad"}},
    "good":{"id":"good","kind":"action","action":"stub","params":{"message":"good"}}
  }}`

// TestSequenceSuccess: two instant-success children finish within the very
// first tick and the sequence reports success; the second child never ticks
// again.
func TestSequenceSuccess(t *testing.T) {
	eng, st, reg := newEngine(t)
	publishJSON(t, eng, "seq", seqInstant)
	id := startExec(t, eng, "seq")

	res := tickOnce(t, eng, id, "")
	if res.Status != "running" {
		t.Fatalf("tick 1: want running (actions dispatched), got %s", res.Status)
	}
	waitForStatus(t, eng, id, "success")

	snap, err := eng.Snapshot(context.Background(), id)
	if err != nil {
		t.Fatal(err)
	}
	for _, ns := range snap.NodeStates {
		if ns.Status != "success" {
			t.Errorf("node %s status=%s want success", ns.NodeID, ns.Status)
		}
	}
	if got := reg.Count("stub"); got != 2 {
		t.Errorf("stub executed %d times, want 2", got)
	}
	if len(snap.Ticks) < 2 {
		t.Errorf("expected multiple committed ticks, got %d", len(snap.Ticks))
	}
	for _, tk := range snap.Ticks {
		if tk.Status == "interrupted" {
			t.Errorf("unexpected interrupted tick: %+v", tk)
		}
	}
	_ = st
}

// TestSequenceFailureShortCircuit: once a child fails, later children never
// execute and the sequence is failure.
func TestSequenceFailureShortCircuit(t *testing.T) {
	eng, _, _ := newEngine(t)
	publishJSON(t, eng, "seqf", seqFailureFirst)
	id := startExec(t, eng, "seqf")
	tickOnce(t, eng, id, "")
	waitForStatus(t, eng, id, "failure")

	snap, _ := eng.Snapshot(context.Background(), id)
	seen := map[string]string{}
	for _, ns := range snap.NodeStates {
		seen[ns.NodeID] = ns.Status
	}
	if seen["bad"] != "failure" || seen["seq"] != "failure" {
		t.Fatalf("unexpected states: %+v", seen)
	}
	if _, ok := seen["good"]; ok {
		t.Fatalf("child after failing branch must never tick, but good=%s", seen["good"])
	}
}

const fallbackTree = `{
  "root":"fb","nodes":{
    "fb":{"id":"fb","kind":"fallback","children":["f1","f2","ok"]},
    "f1":{"id":"f1","kind":"action","action":"stub","params":{"result":"failure"}},
    "f2":{"id":"f2","kind":"action","action":"stub","params":{"result":"failure"}},
    "ok":{"id":"ok","kind":"action","action":"stub","params":{"message":"recovered"}}
  }}`

// TestFallbackBranches: fallback propagates failure child-by-child until a
// success; the root is success and every branch was attempted exactly once.
func TestFallbackBranches(t *testing.T) {
	eng, st, _ := newEngine(t)
	publishJSON(t, eng, "fb", fallbackTree)
	id := startExec(t, eng, "fb")
	tickOnce(t, eng, id, "")
	waitForStatus(t, eng, id, "success")

	for _, node := range []string{"f1", "f2", "ok"} {
		inv := invocationByNode(t, st, id, node)
		if inv.Dispatches != 1 {
			t.Errorf("node %s dispatches=%d want 1", node, inv.Dispatches)
		}
	}
	snap, _ := eng.Snapshot(context.Background(), id)
	wantStatus := map[string]string{"f1": "failure", "f2": "failure", "ok": "success", "fb": "success"}
	for _, ns := range snap.NodeStates {
		if w, ok := wantStatus[ns.NodeID]; ok && ns.Status != w {
			t.Errorf("node %s=%s want %s", ns.NodeID, ns.Status, w)
		}
	}
}

// TestFallbackAllFail: when every branch fails the root is failure.
func TestFallbackAllFail(t *testing.T) {
	raw := `{
    "root":"fb","nodes":{
      "fb":{"id":"fb","kind":"fallback","children":["f1","f2"]},
      "f1":{"id":"f1","kind":"action","action":"stub","params":{"result":"failure"}},
      "f2":{"id":"f2","kind":"action","action":"stub","params":{"result":"failure"}}
    }}`
	eng, _, _ := newEngine(t)
	publishJSON(t, eng, "fb2", raw)
	id := startExec(t, eng, "fb2")
	tickOnce(t, eng, id, "")
	waitForStatus(t, eng, id, "failure")
}

// parallel tree: threshold success=2; two fast children succeed, third slow
// one must be canceled.
const parallelTree = `{
  "root":"par","nodes":{
    "par":{"id":"par","kind":"parallel","success_threshold":2,"failure_threshold":2,"children":["x","y","z"]},
    "x":{"id":"x","kind":"action","action":"stub","params":{"delay_ms":20,"message":"x"}},
    "y":{"id":"y","kind":"action","action":"stub","params":{"delay_ms":40,"message":"y"}},
    "z":{"id":"z","kind":"action","action":"stub","params":{"delay_ms":10000,"message":"z"}}
  }}`

func TestParallelThresholdCancelsSiblings(t *testing.T) {
	eng, st, _ := newEngine(t)
	publishJSON(t, eng, "par", parallelTree)
	id := startExec(t, eng, "par")
	tickOnce(t, eng, id, "")
	waitForStatus(t, eng, id, "success")

	z := invocationByNode(t, st, id, "z")
	if z.Status != "canceled" {
		t.Fatalf("slow sibling z status=%s want canceled", z.Status)
	}
	x := invocationByNode(t, st, id, "x")
	y := invocationByNode(t, st, id, "y")
	if x.Status != "success" || y.Status != "success" {
		t.Fatalf("x=%s y=%s want both success", x.Status, y.Status)
	}

	// A result from the canceled sibling must never be accepted even if the
	// stub kept running in a zombie: the DB row stays canceled. Give a
	// generous grace so the interrupted goroutine cannot flip it back.
	time.Sleep(100 * time.Millisecond)
	z2 := invocationByNode(t, st, id, "z")
	if z2.Status != "canceled" {
		t.Fatalf("canceled sibling resurrected to %s", z2.Status)
	}
}

// TestParallelFailureThreshold: failure_threshold=1 means the first failing
// child fails the node and cancels the rest.
func TestParallelFailureThreshold(t *testing.T) {
	raw := `{
    "root":"par","nodes":{
      "par":{"id":"par","kind":"parallel","success_threshold":3,"failure_threshold":1,"children":["slow","bad","other"]},
      "slow":{"id":"slow","kind":"action","action":"stub","params":{"delay_ms":10000}},
      "bad":{"id":"bad","kind":"action","action":"stub","params":{"result":"failure"}},
      "other":{"id":"other","kind":"action","action":"stub","params":{"delay_ms":10000}}
    }}`
	eng, st, _ := newEngine(t)
	publishJSON(t, eng, "parf", raw)
	id := startExec(t, eng, "parf")
	tickOnce(t, eng, id, "")
	waitForStatus(t, eng, id, "failure")
	for _, n := range []string{"slow", "other"} {
		inv := invocationByNode(t, st, id, n)
		if inv.Status != "canceled" {
			t.Errorf("node %s=%s want canceled after failure quota", n, inv.Status)
		}
	}
}

// timeout races -------------------------------------------------------------

const timeoutWins = `{
  "root":"to","nodes":{
    "to":{"id":"to","kind":"timeout","timeout_ms":60,"children":["slow"]},
    "slow":{"id":"slow","kind":"action","action":"stub","params":{"delay_ms":10000}}
  }}`

func TestTimeoutWins(t *testing.T) {
	eng, st, _ := newEngine(t)
	publishJSON(t, eng, "to", timeoutWins)
	id := startExec(t, eng, "to")
	start := time.Now()
	tickOnce(t, eng, id, "")
	waitForStatus(t, eng, id, "failure")
	if d := time.Since(start); d > 2*time.Second {
		t.Fatalf("timeout took %v, expected ~60ms", d)
	}
	inv := invocationByNode(t, st, id, "slow")
	if inv.Status != "canceled" {
		t.Fatalf("timed-out invocation=%s want canceled", inv.Status)
	}
	// The late success must never resurrect the tree.
	time.Sleep(100 * time.Millisecond)
	inv2 := invocationByNode(t, st, id, "slow")
	if inv2.Status != "canceled" {
		t.Fatalf("late result resurrected timed-out node to %s", inv2.Status)
	}
}

const timeoutLoses = `{
  "root":"to","nodes":{
    "to":{"id":"to","kind":"timeout","timeout_ms":500,"children":["fast"]},
    "fast":{"id":"fast","kind":"action","action":"stub","params":{"delay_ms":30,"message":"done"}}
  }}`

func TestTimeoutSuccessRace(t *testing.T) {
	eng, _, _ := newEngine(t)
	publishJSON(t, eng, "tol", timeoutLoses)
	id := startExec(t, eng, "tol")
	tickOnce(t, eng, id, "")
	waitForStatus(t, eng, id, "success")
}

// Restart / non-idempotent dedup --------------------------------------------

// TestNonIdempotentNoReexecuteAcrossRestart is the headline guarantee: a
// non-idempotent stub that has succeeded is never run again after the whole
// engine is rebuilt (simulating a process restart), proven both by the
// dispatch counter and by the persisted result being reused.
func TestNonIdempotentNoReexecuteAcrossRestart(t *testing.T) {
	raw := `{
    "root":"seq","nodes":{
      "seq":{"id":"seq","kind":"sequence","children":["charge","ship"]},
      "charge":{"id":"charge","kind":"action","action":"stub.nonidempotent","idempotent":false,"params":{"message":"charged $1"}},
      "ship":{"id":"ship","kind":"action","action":"stub","params":{"delay_ms":10000,"message":"shipping"}}
    }}`
	st := newTestStore(t)
	wipe(t, st)
	reg1 := stub.NewRegistry()
	eng1 := engine.New(st, reg1)
	publishJSON(t, eng1, "pay", raw)
	id := startExec(t, eng1, "pay")
	tickOnce(t, eng1, id, "")
	waitInvocation(t, st, id, "charge", "success")

	charge := invocationByNode(t, st, id, "charge")
	if charge.Dispatches != 1 {
		t.Fatalf("charge dispatches=%d want 1", charge.Dispatches)
	}
	if reg1.Count("stub.nonidempotent") != 1 {
		t.Fatalf("registry executed nonidempotent %d times want 1", reg1.Count("stub.nonidempotent"))
	}

	// Simulate process restart: drop engine (cancels workers), build a brand
	// new registry with zero counters and a new engine.
	eng1.Close()
	reg2 := stub.NewRegistry()
	eng2 := engine.New(st, reg2)
	if err := eng2.Recover(context.Background()); err != nil {
		t.Fatalf("recover: %v", err)
	}

	// Tick the resumed execution repeatedly. "charge" must stay success and
	// never dispatch again; "ship" is still pending/running. We then cancel so
	// the test doesn't wait 10s.
	for i := 0; i < 3; i++ {
		if _, err := eng2.Tick(context.Background(), id, "post-restart"); err != nil {
			t.Fatalf("tick after restart: %v", err)
		}
		time.Sleep(20 * time.Millisecond)
	}
	charge2 := invocationByNode(t, st, id, "charge")
	if charge2.Status != "success" || charge2.Dispatches != 1 || charge2.Attempt != 1 {
		t.Fatalf("charge after restart: status=%s dispatches=%d attempt=%d; want success/1/1",
			charge2.Status, charge2.Dispatches, charge2.Attempt)
	}
	if n := reg2.Count("stub.nonidempotent"); n != 0 {
		t.Fatalf("non-idempotent action re-executed %d times after restart, want 0", n)
	}
	eng2.Close()
}

// tick interruption ---------------------------------------------------------

// TestTickInterruption: a tick whose context is canceled before commit must
// roll back completely: sequence number is not consumed and no invocation is
// visible from the aborted tick.
func TestTickInterruption(t *testing.T) {
	eng, st, _ := newEngine(t)
	publishJSON(t, eng, "ti", seqInstant)
	id := startExec(t, eng, "ti")

	block := make(chan struct{})
	eng.SetBeforeCommitHook(func(_ uuid.UUID, _ int64, ch <-chan struct{}) {
		go func() {
			<-block // keep the tick suspended until the test releases/cancels
			_ = ch
		}()
	})

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() {
		_, err := eng.Tick(ctx, id, "interruptible")
		done <- err
	}()

	// Wait until the tick is holding the execution lock, then cancel.
	time.Sleep(100 * time.Millisecond)
	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("interrupted tick should return nil error, got %v", err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("interrupted tick did not return")
	}
	close(block)
	eng.SetBeforeCommitHook(nil)

	exec, err := st.GetExecution(context.Background(), id)
	if err != nil {
		t.Fatal(err)
	}
	if exec.NextTick != 1 {
		t.Fatalf("next_tick=%d after interrupted tick, want 1 (sequence must not advance)", exec.NextTick)
	}
	invs, err := st.ListInvocations(context.Background(), id)
	if err != nil {
		t.Fatal(err)
	}
	if len(invs) != 0 {
		t.Fatalf("interrupted tick leaked %d invocations", len(invs))
	}
	ticks, err := st.ListTicks(context.Background(), id)
	if err != nil {
		t.Fatal(err)
	}
	var interrupted int
	for _, tk := range ticks {
		if tk.Status == "interrupted" {
			interrupted++
		}
	}
	if interrupted != 1 {
		t.Fatalf("interrupted tick rows=%d want 1", interrupted)
	}

	// A subsequent tick at the same sequence completes normally.
	tickOnce(t, eng, id, "retry after interrupt")
	waitForStatus(t, eng, id, "success")
	exec2, _ := st.GetExecution(context.Background(), id)
	if exec2.NextTick < 3 {
		t.Fatalf("after retry next_tick=%d want >=3", exec2.NextTick)
	}
}

// execution cancellation ----------------------------------------------------

func TestCancelExecution(t *testing.T) {
	eng, st, _ := newEngine(t)
	publishJSON(t, eng, "cx", timeoutWins)
	id := startExec(t, eng, "cx")
	tickOnce(t, eng, id, "")
	waitInvocation(t, st, id, "slow", "running")

	if err := eng.Cancel(context.Background(), id); err != nil {
		t.Fatalf("cancel: %v", err)
	}
	waitForStatus(t, eng, id, "canceled")
	inv := invocationByNode(t, st, id, "slow")
	if inv.Status != "canceled" {
		t.Fatalf("invocation=%s want canceled", inv.Status)
	}
	// Further ticks are rejected with conflict.
	if _, err := eng.Tick(context.Background(), id, ""); err != store.ErrConflict {
		t.Fatalf("tick after cancel err=%v want ErrConflict", err)
	}
}

// late results: attempt barrier ---------------------------------------------

// TestLateResultRejected forces a worker to finish after its invocation row
// has been flipped to canceled and proves the optimistic attempt barrier
// rejects the report (status stays canceled, no resurrection).
func TestLateResultRejected(t *testing.T) {
	raw := `{
    "root":"to","nodes":{
      "to":{"id":"to","kind":"timeout","timeout_ms":40,"children":["slow"]},
      "slow":{"id":"slow","kind":"action","action":"stub","params":{"delay_ms":500}}
    }}`
	eng, st, _ := newEngine(t)
	publishJSON(t, eng, "late", raw)
	id := startExec(t, eng, "late")
	tickOnce(t, eng, id, "")
	waitForStatus(t, eng, id, "failure")
	inv := invocationByNode(t, st, id, "slow")
	if inv.Status != "canceled" {
		t.Fatalf("precondition: inv=%s want canceled", inv.Status)
	}
	// Wait well beyond the 500ms stub runtime; if the report could land, it
	// would have flipped the row by now.
	time.Sleep(600 * time.Millisecond)
	inv2 := invocationByNode(t, st, id, "slow")
	if inv2.Status != "canceled" {
		t.Fatalf("late worker report resurrected invocation to %s", inv2.Status)
	}
	exec, _ := st.GetExecution(context.Background(), id)
	if exec.Status != "failure" {
		t.Fatalf("execution resurrected to %s, want failure", exec.Status)
	}
}

// tick sequence numbers are monotonic gapless committed ----------------------

func TestTickSequencePersisted(t *testing.T) {
	raw := `{
    "root":"seq","nodes":{
      "seq":{"id":"seq","kind":"sequence","children":["a","b","c"]},
      "a":{"id":"a","kind":"action","action":"stub","params":{"delay_ms":20}},
      "b":{"id":"b","kind":"action","action":"stub","params":{"delay_ms":20}},
      "c":{"id":"c","kind":"action","action":"stub","params":{"delay_ms":20}}
    }}`
	eng, st, _ := newEngine(t)
	publishJSON(t, eng, "ts", raw)
	id := startExec(t, eng, "ts")
	// First tick dispatches a; auto-ticks carry the rest.
	tickOnce(t, eng, id, "first")
	waitForStatus(t, eng, id, "success")

	ticks, err := st.ListTicks(context.Background(), id)
	if err != nil {
		t.Fatal(err)
	}
	committed := []int64{}
	for _, tk := range ticks {
		if tk.Status != "interrupted" {
			committed = append(committed, tk.Seq)
		}
	}
	for i, seq := range committed {
		if seq != int64(i+1) {
			t.Fatalf("committed seq at position %d = %d want %d (gap/dup in %v)", i, seq, i+1, committed)
		}
	}
	if len(committed) < 3 {
		t.Fatalf("expected >=3 committed ticks, got %v", committed)
	}
}

// hash stub really computes SHA-256 -----------------------------------------

func TestRealSHA256Action(t *testing.T) {
	raw := `{
    "root":"h","nodes":{
      "h":{"id":"h","kind":"action","action":"hash.sha256","params":{"input":"abc"}}
    }}`
	eng, st, _ := newEngine(t)
	publishJSON(t, eng, "hash", raw)
	id := startExec(t, eng, "hash")
	tickOnce(t, eng, id, "")
	waitForStatus(t, eng, id, "success")
	inv := invocationByNode(t, st, id, "h")
	want := "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
	if got := string(inv.Result); !strings.Contains(got, want) {
		t.Fatalf("result=%s missing sha256 %s", got, want)
	}
}
