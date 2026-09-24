package core

import (
	"errors"
	"testing"
)

func mustCtrl(t *testing.T, r CtrlResult, err error) map[string]any {
	t.Helper()
	if err != nil {
		t.Fatalf("unexpected control error: %v", err)
	}
	return r.Body
}

// TestPhaseOrdering enforces the legal state-machine transitions directly.
func TestPhaseOrdering(t *testing.T) {
	c := NewCluster()

	if _, err := c.CompleteSnapshot("x", nil, 0); !errors.Is(err, ErrBadPhase) {
		t.Fatalf("complete_snapshot before begin: want ErrBadPhase, got %v", err)
	}
	if _, err := c.CatchUp("x", nil, 0); !errors.Is(err, ErrBadPhase) {
		t.Fatalf("catch_up before snapshot: want ErrBadPhase, got %v", err)
	}
	if _, err := c.PrepareCutover("x"); !errors.Is(err, ErrBadPhase) {
		t.Fatalf("prepare before catchup: want ErrBadPhase, got %v", err)
	}
	if _, err := c.CommitCutover("x"); !errors.Is(err, ErrBadPhase) {
		t.Fatalf("commit before prepare: want ErrBadPhase, got %v", err)
	}

	r, err := c.BeginSnapshot("b1")
	mustCtrl(t, r, err)
	// snapshot point captures the last seq at begin time
	if _, _, err := c.Append(NodeA, 1, "k1", "v"); err != nil {
		t.Fatal(err)
	}
	st := c.Snapshot()
	if st.SnapshotSeq != 0 || st.LastSeq != 1 {
		t.Fatalf("snapshot should pin seq 0, got snap=%d last=%d", st.SnapshotSeq, st.LastSeq)
	}

	// transfer k1 as the snapshot batch and complete
	log, _ := c.LogRange(NodeA, 0, st.SnapshotSeq)
	if len(log) != 0 {
		t.Fatalf("range (0,0] must be empty, got %d", len(log))
	}
	// simulate install of post-snapshot-point write through catch-up instead
	if _, err := c.CompleteSnapshot("b2", nil, 0); err != nil {
		t.Fatalf("complete snapshot: %v", err)
	}
	if c.Snapshot().Phase != PhaseCatchUp {
		t.Fatal("phase should be catchup")
	}

	// catch-up refuses while B lags guard is only enforced at prepare;
	// drain k1 now
	tail, _ := c.LogRange(NodeA, 0, -1)
	if len(tail) != 1 {
		t.Fatalf("tail should contain k1, got %d", len(tail))
	}
	r, err = c.CatchUp("c1", tail, len(tail))
	mustCtrl(t, r, err)

	// prepare rejects if B still lags — here it does not
	r, err = c.PrepareCutover("p1")
	mustCtrl(t, r, err)
	// writes frozen
	if _, _, err := c.Append(NodeA, 1, "frozen", "v"); !errors.Is(err, ErrClusterFrozen) {
		t.Fatalf("append in switching: want ErrClusterFrozen, got %v", err)
	}
	// B with epoch 2 still cannot write before commit
	if _, _, err := c.Append(NodeB, 2, "early", "v"); !errors.Is(err, ErrClusterFrozen) {
		t.Fatalf("B append in switching: want ErrClusterFrozen, got %v", err)
	}
	r, err = c.CommitCutover("co1")
	mustCtrl(t, r, err)
	st = c.Snapshot()
	if st.Epoch != 2 || st.Primary != NodeB || st.Phase != PhaseDone {
		t.Fatalf("post-commit state wrong: %+v", st)
	}
}

// TestFencingAfterSwitch checks every route combination after cutover.
func TestFencingAfterSwitch(t *testing.T) {
	c := NewCluster()
	r2, e2 := c.BeginSnapshot("b")
	mustCtrl(t, r2, e2)
	r2, e2 = c.CompleteSnapshot("s", nil, 0)
	mustCtrl(t, r2, e2)
	r2, e2 = c.CatchUp("c", nil, 0)
	mustCtrl(t, r2, e2)
	r2, e2 = c.PrepareCutover("p")
	mustCtrl(t, r2, e2)
	r2, e2 = c.CommitCutover("co")
	mustCtrl(t, r2, e2)

	// A on old epoch -> stale
	if _, _, err := c.Append(NodeA, 1, "a1", "v"); !errors.Is(err, ErrStaleEpoch) {
		t.Fatalf("A epoch1: want stale, got %v", err)
	}
	// A pretending to know new epoch -> not primary (checked after epoch)
	if _, _, err := c.Append(NodeA, 2, "a2", "v"); !errors.Is(err, ErrNotPrimary) {
		t.Fatalf("A epoch2: want not_primary, got %v", err)
	}
	// B on old epoch -> stale
	if _, _, err := c.Append(NodeB, 1, "b1", "v"); !errors.Is(err, ErrStaleEpoch) {
		t.Fatalf("B epoch1: want stale, got %v", err)
	}
	// B epoch 2 -> accepted
	res, _, err := c.Append(NodeB, 2, "b2", "v")
	if err != nil {
		t.Fatalf("B epoch2 append: %v", err)
	}
	if res.Primary != NodeB || res.Epoch != 2 {
		t.Fatalf("B append provenance wrong: %+v", res)
	}
}

// TestVerifyCatchesDualConfirmation constructs an impossible state directly
// and ensures the verifier flags a key confirmed by two primaries.
func TestVerifyCatchesDualConfirmation(t *testing.T) {
	c := NewCluster()
	c.lastSeq = 1
	c.nodes[NodeA].log = []*Write{{Seq: 1, Epoch: 1, Primary: NodeA, Key: "dup", Value: ""}}
	c.nodes[NodeB].log = []*Write{{Seq: 2, Epoch: 2, Primary: NodeB, Key: "dup", Value: ""}}
	c.phase = PhaseDone

	checks := c.Verify()
	byName := map[string]bool{}
	for _, ck := range checks {
		byName[ck.Name] = ck.Pass
	}
	if byName["no_dual_primary_confirmation"] {
		t.Fatal("verifier must detect dual-primary confirmation")
	}
}

// TestCommandIdempotencyCache checks replay semantics at the core boundary.
func TestCommandIdempotencyCache(t *testing.T) {
	c := NewCluster()
	r1, err := c.BeginSnapshot("id-1")
	if err != nil || r1.Replayed {
		t.Fatalf("first call: %+v %v", r1, err)
	}
	r2, err := c.BeginSnapshot("id-1")
	if err != nil || !r2.Replayed {
		t.Fatalf("duplicate call must replay: %+v %v", r2, err)
	}
	// a different command id in a later phase sees the advancement, not an error
	c.CompleteSnapshot("id-2", nil, 0)
	r3, _ := c.BeginSnapshot("id-1")
	if !r3.Replayed || r3.Body["phase"] != PhaseSnapshot {
		t.Fatalf("replay must preserve original body: %+v", r3)
	}
}
