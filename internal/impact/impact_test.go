package impact

import (
	"testing"
	"time"

	"bis/internal/builder"
	"bis/internal/spec"
	"bis/internal/store"
	"bis/internal/testutil"
)

func buildShared(t *testing.T) (*store.Store, *spec.Project) {
	t.Helper()
	st := testutil.NewStore(t)
	p := testutil.InstallSharedProject(t, st)
	b := builder.New(st, builder.WithTimeout(10*time.Second))
	if _, err := b.Build(p); err != nil {
		t.Fatal(err)
	}
	return st, p
}

func outputSet(rep *Report) map[string]map[string]bool {
	out := map[string]map[string]bool{}
	for _, o := range rep.AffectedOutputs {
		if out[o.ActionID] == nil {
			out[o.ActionID] = map[string]bool{}
		}
		out[o.ActionID][o.Output] = true
	}
	return out
}

func TestImpactSharedSourceBlastRadius(t *testing.T) {
	st, p := buildShared(t)
	rep := Analyze(st, p, "src/shared.txt", nil)

	if len(rep.MatchedSources) != 1 || rep.MatchedSources[0] != "src/shared.txt" {
		t.Fatalf("matched sources = %v", rep.MatchedSources)
	}
	got := outputSet(rep)
	for _, a := range []string{"compile_a", "compile_b", "link"} {
		if len(got[a]) == 0 {
			t.Errorf("expected %s to be affected by shared.txt", a)
		}
	}
	for _, o := range rep.AffectedOutputs {
		if o.Distance < 1 {
			t.Error("distance must start at 1")
		}
	}
	// At least one chain must span a diamond edge into the link action.
	foundLink := false
	for _, c := range rep.Chains {
		if c.Output == "out/final.txt" && len(c.Path) >= 2 && c.Path[len(c.Path)-1] == "link" {
			foundLink = true
		}
	}
	if !foundLink {
		t.Fatalf("no shared-source -> link chain found: %+v", rep.Chains)
	}
}

func TestImpactBranchSourceScopedToOneBranch(t *testing.T) {
	st, p := buildShared(t)
	rep := Analyze(st, p, "src/a.txt", nil)
	got := outputSet(rep)
	if len(got["compile_a"]) == 0 {
		t.Error("compile_a must be affected by a.txt")
	}
	if len(got["compile_b"]) != 0 {
		t.Errorf("compile_b must NOT be affected by a.txt, got %v", got["compile_b"])
	}
	if len(got["link"]) == 0 {
		t.Error("link transitively consumes a.txt through compile_a")
	}
}

func TestImpactByDigest(t *testing.T) {
	st, p := buildShared(t)
	idx := st.LoadIndex(p.Name)
	rec, err := st.GetRecord(idx.Records["compile_a"].RecordID)
	if err != nil {
		t.Fatal(err)
	}
	var d = rec.Sources[0].Digest
	for _, s := range rec.Sources {
		if s.Path == "src/a.txt" {
			d = s.Digest
		}
	}
	rep := Analyze(st, p, "", &d)
	if len(rep.MatchedSources) != 1 || rep.MatchedSources[0] != "src/a.txt" {
		t.Fatalf("digest query matched %v", rep.MatchedSources)
	}
}

func TestImpactUnknownSourceIsEmpty(t *testing.T) {
	st, p := buildShared(t)
	rep := Analyze(st, p, "src/does-not-exist.txt", nil)
	if len(rep.AffectedActions) != 0 {
		t.Fatalf("unknown source should affect nothing, got %v", rep.AffectedActions)
	}
}
