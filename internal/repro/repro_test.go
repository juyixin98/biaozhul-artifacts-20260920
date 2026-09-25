package repro

import (
	"os"
	"testing"
	"time"

	"bis/internal/builder"
	"bis/internal/testutil"
	"bis/internal/verify"
)

func TestDeterministicBuildReproduces(t *testing.T) {
	st := testutil.NewStore(t)
	p := testutil.InstallSharedProject(t, st)
	if _, err := builder.New(st).Build(p); err != nil {
		t.Fatal(err)
	}
	rep, cleanup, err := Run(st, p, 10*time.Second, time.Now)
	defer cleanup()
	if err != nil {
		t.Fatal(err)
	}
	if !rep.Reproduced {
		for _, a := range rep.Actions {
			t.Errorf("action %s status=%s err=%s comparisons=%+v", a.ActionID, a.Status, a.Error, a.Outputs)
		}
	}
	// The rerun genuinely executed fixture commands in its isolated root.
	if len(rep.CommandsRun) != 3 {
		t.Fatalf("rerun executed %d commands, want 3", len(rep.CommandsRun))
	}
}

func TestNondeterministicBuildDoesNotReproduce(t *testing.T) {
	st := testutil.NewStore(t)
	p := testutil.InstallNondetProject(t, st)
	if _, err := builder.New(st).Build(p); err != nil {
		t.Fatal(err)
	}
	rep, cleanup, err := Run(st, p, 10*time.Second, time.Now)
	defer cleanup()
	if err != nil {
		t.Fatal(err)
	}
	if rep.Reproduced {
		t.Fatal("a fresh UUID per run must prevent byte-identical reproduction")
	}
	foundDifferent := false
	for _, a := range rep.Actions {
		if a.Status == "different" {
			foundDifferent = true
			for _, c := range a.Outputs {
				if c.OriginalDigest == c.RerunDigest {
					t.Error("digests unexpectedly equal")
				}
			}
		}
	}
	if !foundDifferent {
		t.Fatalf("expected a 'different' action, got %+v", rep.Actions)
	}
}

func TestReproUsesIsolatedStore(t *testing.T) {
	st := testutil.NewStore(t)
	p := testutil.InstallSharedProject(t, st)
	if _, err := builder.New(st).Build(p); err != nil {
		t.Fatal(err)
	}
	rep, cleanup, err := Run(st, p, 10*time.Second, time.Now)
	defer cleanup()
	if err != nil {
		t.Fatal(err)
	}
	if rep.Rerun == nil || rep.Rerun.Status != "ok" {
		t.Fatalf("rerun failed: %+v", rep.Rerun)
	}
	for _, ea := range rep.Rerun.Actions {
		if ea.Status != "executed" {
			t.Fatalf("isolated rerun must be cold (no shared cache), action %s=%s", ea.ActionID, ea.Status)
		}
	}
}

func TestCompleteProvenanceButNotReproducible(t *testing.T) {
	// The nondeterministic fixture has a complete, valid signed chain but
	// every rerun embeds a fresh UUID, so the output digest differs.
	st := testutil.NewStore(t)
	p := testutil.InstallNondetProject(t, st)
	if _, err := builder.New(st).Build(p); err != nil {
		t.Fatal(err)
	}
	vrep, err := verify.New(st).Verify(p)
	if err != nil {
		t.Fatal(err)
	}
	if !vrep.Complete {
		t.Fatalf("provenance must be complete despite nondeterminism: %+v", vrep.Findings)
	}
	rrep, cleanup, err := Run(st, p, 10*time.Second, time.Now)
	defer cleanup()
	if err != nil {
		t.Fatal(err)
	}
	if rrep.Reproduced {
		t.Fatal("nondeterministic output must not reproduce")
	}
}

func TestIncompleteProvenanceButStillReproducible(t *testing.T) {
	// Delete the base artifact blob from the cache. The provenance chain is
	// now incomplete (BLOB_MISSING, child tainted), yet a clean isolated
	// rerun produces byte-identical outputs: reproducibility is orthogonal
	// to chain completeness.
	st := testutil.NewStore(t)
	p := testutil.InstallTwoNodeProject(t, st)
	if _, err := builder.New(st).Build(p); err != nil {
		t.Fatal(err)
	}
	idx := st.LoadIndex(p.Name)
	baseOut := idx.Records["base"].Outputs["out/base.txt"]
	if err := os.Remove(st.BlobPath(baseOut)); err != nil {
		t.Fatal(err)
	}
	vrep, err := verify.New(st).Verify(p)
	if err != nil {
		t.Fatal(err)
	}
	if vrep.Complete {
		t.Fatal("chain must be incomplete after blob deletion")
	}
	rrep, cleanup, err := Run(st, p, 10*time.Second, time.Now)
	defer cleanup()
	if err != nil {
		t.Fatal(err)
	}
	if !rrep.Reproduced {
		t.Fatalf("deterministic tool must still reproduce despite broken chain: %+v", rrep.Actions)
	}
}
