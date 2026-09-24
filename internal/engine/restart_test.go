package engine_test

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"

	"resumable-bt/internal/engine"
	"resumable-bt/internal/store"
	"resumable-bt/internal/stub"
)

func errorIsConflict(err error) bool { return errors.Is(err, store.ErrConflict) }

// TestTimeoutDeadlineSurvivesRestart: the timeout deadline is fixed at first
// tick and persisted; after an engine restart the remaining budget — not the
// full timeout — applies, so an action that ran long before the crash is
// still failed by the original deadline.
func TestTimeoutDeadlineSurvivesRestart(t *testing.T) {
	raw := `{
    "root":"to","nodes":{
      "to":{"id":"to","kind":"timeout","timeout_ms":300,"children":["slow"]},
      "slow":{"id":"slow","kind":"action","action":"stub","params":{"delay_ms":60000}}
    }}`
	st := newTestStore(t)
	wipe(t, st)
	reg1 := stub.NewRegistry()
	eng1 := engine.New(st, reg1)
	publishJSON(t, eng1, "tdead", raw)
	id := startExec(t, eng1, "tdead")
	t0 := time.Now()
	tickOnce(t, eng1, id, "")
	waitInvocation(t, st, id, "slow", "running")

	// Wait 200ms (past 2/3 of the 300ms budget), then "restart".
	time.Sleep(200 * time.Millisecond)
	eng1.Close()

	reg2 := stub.NewRegistry()
	eng2 := engine.New(st, reg2)
	defer eng2.Close()
	if err := eng2.Recover(context.Background()); err != nil {
		t.Fatal(err)
	}

	exec := waitForStatus(t, eng2, id, "failure")
	elapsed := time.Since(t0)
	if elapsed > 1500*time.Millisecond {
		t.Fatalf("timeout after restart took %v; deadline should have survived near 300ms", elapsed)
	}
	if elapsed < 250*time.Millisecond {
		t.Fatalf("timed out at %v, before the original 300ms deadline", elapsed)
	}
	inv := invocationByNode(t, st, id, "slow")
	if inv.Status != "canceled" {
		t.Fatalf("slow invocation=%s want canceled", inv.Status)
	}
	_ = exec
}

// TestConcurrentTicksSerialized: many simultaneous Tick calls must be
// serialized by the execution lock and produce a gapless committed sequence;
// no tick may observe a stale tree state.
func TestConcurrentTicksSerialized(t *testing.T) {
	raw := `{
    "root":"seq","nodes":{
      "seq":{"id":"seq","kind":"sequence","children":["a"]},
      "a":{"id":"a","kind":"action","action":"stub","params":{"message":"only"}}
    }}`
	eng, st, _ := newEngine(t)
	publishJSON(t, eng, "conc", raw)
	id := startExec(t, eng, "conc")

	var wg sync.WaitGroup
	errs := make(chan error, 8)
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, err := eng.Tick(context.Background(), id, "concurrent")
			if err != nil && !errorIsConflict(err) {
				errs <- err
			}
		}()
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Fatalf("concurrent tick error: %v", err)
	}
	// Auto-ticks finish the action; wait for terminal and verify exactly one
	// dispatch despite the tick storm.
	waitForStatus(t, eng, id, "success")
	inv := invocationByNode(t, st, id, "a")
	if inv.Dispatches != 1 {
		t.Fatalf("dispatches=%d under concurrent ticks, want 1", inv.Dispatches)
	}
	ticks, _ := st.ListTicks(context.Background(), id)
	committed := map[int64]bool{}
	for _, tk := range ticks {
		if tk.Status != "interrupted" {
			if committed[tk.Seq] {
				t.Fatalf("duplicate committed seq %d", tk.Seq)
			}
			committed[tk.Seq] = true
		}
	}
	for i := int64(1); i <= int64(len(committed)); i++ {
		if !committed[i] {
			t.Fatalf("gap in committed sequence at %d: %v", i, committed)
		}
	}
}

// TestSequenceWithFallbackResumeMidTree: a sequence containing a fallback
// resumes exactly at the active fallback branch across ticks without
// restarting earlier successful work.
func TestFallbackResumePosition(t *testing.T) {
	eng, st, reg := newEngine(t)

	// Register a deterministic gating action: it stays running until release.
	release := make(chan struct{})
	started := make(chan struct{}, 1)
	reg.Register("gated.fail", false, func(ctx context.Context, _ map[string]any) (any, error) {
		started <- struct{}{}
		select {
		case <-release:
			return nil, fmt.Errorf("gated failure")
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	})

	raw := `{
    "root":"seq","nodes":{
      "seq":{"id":"seq","kind":"sequence","children":["first","fb"]},
      "first":{"id":"first","kind":"action","action":"stub","params":{"message":"first"}},
      "fb":{"id":"fb","kind":"fallback","children":["gated","later_ok"]},
      "gated":{"id":"gated","kind":"action","action":"gated.fail"},
      "later_ok":{"id":"later_ok","kind":"action","action":"stub","params":{"message":"ok"}}
    }}`
	publishJSON(t, eng, "fr", raw)
	id := startExec(t, eng, "fr")
	tickOnce(t, eng, id, "")
	<-started

	// While the gated branch is running, later_ok must not be dispatched.
	for i := 0; i < 10; i++ {
		rows, _ := st.ListInvocations(context.Background(), id)
		if len(rows) != 2 {
			t.Fatalf("before gate release, expected 2 invocations (first+gated), got %d", len(rows))
		}
		if invocationByNode(t, st, id, "gated").Status != "running" {
			t.Fatalf("gated branch should be running")
		}
		time.Sleep(5 * time.Millisecond)
	}

	close(release)
	waitForStatus(t, eng, id, "success")
	for _, n := range []string{"first", "later_ok"} {
		if invocationByNode(t, st, id, n).Status != "success" {
			t.Fatalf("node %s want success", n)
		}
	}
	if invocationByNode(t, st, id, "gated").Status != "failure" {
		t.Fatalf("gated want failure")
	}
	// first dispatched exactly once even though many ticks re-walked the root.
	if d := invocationByNode(t, st, id, "first").Dispatches; d != 1 {
		t.Fatalf("first dispatches=%d want 1", d)
	}
}
