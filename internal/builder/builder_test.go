package builder

import (
	"os"
	"path/filepath"
	"testing"
	"time"

	"bis/internal/spec"
	"bis/internal/testutil"
)

func buildOK(t *testing.T, p *spec.Project) (*Builder, *Report) {
	t.Helper()
	st := testutil.NewStore(t)
	b := New(st, WithTimeout(10*time.Second))
	rep, err := b.Build(p)
	if err != nil {
		t.Fatalf("Build() error: %v", err)
	}
	if rep.Status != "ok" {
		t.Fatalf("Build() status = %q, want ok", rep.Status)
	}
	return b, rep
}

func TestBuildDiamondExecutesInOrder(t *testing.T) {
	st := testutil.NewStore(t)
	p := testutil.InstallSharedProject(t, st)
	b := New(st)
	rep, err := b.Build(p)
	if err != nil {
		t.Fatalf("Build() error: %v", err)
	}
	order := map[string]int{}
	for i, ea := range rep.Actions {
		if ea.Status != "executed" {
			t.Fatalf("action %s status = %s (%s)", ea.ActionID, ea.Status, ea.Error)
		}
		order[ea.ActionID] = i
	}
	if !(order["compile_a"] < order["link"] && order["compile_b"] < order["link"]) {
		t.Fatalf("link did not run after its upstreams: %v", order)
	}
	if len(rep.CommandsRun) != 3 {
		t.Fatalf("CommandsRun = %v, want 3 fixture executions", rep.CommandsRun)
	}
	// The final artifact must contain content traceable to all three sources.
	idx := st.LoadIndex(p.Name)
	link := idx.Records["link"]
	r, err := st.GetRecord(link.RecordID)
	if err != nil {
		t.Fatal(err)
	}
	if len(r.Upstreams) != 2 {
		t.Fatalf("link has %d upstream refs, want 2", len(r.Upstreams))
	}
	finalPath := st.BlobPath(link.Outputs["out/final.txt"])
	data, err := os.ReadFile(finalPath)
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{"COMMON HEADER", "alpha-only", "bravo-only"} {
		if !contains(string(data), want) {
			t.Errorf("final output missing %q", want)
		}
	}
}

func TestBuildCachesOnSecondRun(t *testing.T) {
	st := testutil.NewStore(t)
	p := testutil.InstallSharedProject(t, st)
	b := New(st)
	if _, err := b.Build(p); err != nil {
		t.Fatal(err)
	}
	rep2, err := b.Build(p)
	if err != nil {
		t.Fatal(err)
	}
	for _, ea := range rep2.Actions {
		if ea.Status != "cached" {
			t.Fatalf("action %s status = %s, want cached", ea.ActionID, ea.Status)
		}
	}
	if len(rep2.CommandsRun) != 0 {
		t.Fatalf("cache hit still executed commands: %v", rep2.CommandsRun)
	}
}

func TestSourceChangeInvalidatesCache(t *testing.T) {
	st := testutil.NewStore(t)
	p := testutil.InstallSharedProject(t, st)
	b := New(st)
	if _, err := b.Build(p); err != nil {
		t.Fatal(err)
	}
	// Touch a source; compile_b and link must re-execute, compile_a stays cached.
	testutil.Overwrite(t, filepath.Join(st.ProjectRoot(p.Name), "src", "b.txt"), "bravo CHANGED\n")
	rep, err := b.Build(p)
	if err != nil {
		t.Fatal(err)
	}
	status := map[string]string{}
	for _, ea := range rep.Actions {
		status[ea.ActionID] = ea.Status
	}
	if status["compile_a"] != "cached" {
		t.Errorf("compile_a = %s, want cached", status["compile_a"])
	}
	if status["compile_b"] != "executed" {
		t.Errorf("compile_b = %s, want executed", status["compile_b"])
	}
	if status["link"] != "executed" {
		t.Errorf("link = %s, want executed", status["link"])
	}
}

func TestBuildFailureSkipsDownstream(t *testing.T) {
	st := testutil.NewStore(t)
	p := testutil.InstallFailProject(t, st)
	b := New(st)
	rep, err := b.Build(p)
	if err == nil {
		t.Fatal("expected build failure")
	}
	if rep.Status != "failed" || rep.Actions[0].Status != "failed" {
		t.Fatalf("unexpected report: %+v", rep)
	}
}

func TestWorkDirDisposableAndSeparateFromCache(t *testing.T) {
	st := testutil.NewStore(t)
	p := testutil.InstallSharedProject(t, st)
	b := New(st)
	if _, err := b.Build(p); err != nil {
		t.Fatal(err)
	}
	// Work tree must be empty once the run finishes...
	entries, _ := os.ReadDir(st.WorkDir)
	if len(entries) != 0 {
		t.Fatalf("work dir not cleaned up: %v", entries)
	}
	// ...while the immutable cache still holds blobs.
	blobs, _ := os.ReadDir(st.BlobsDir)
	if len(blobs) == 0 {
		t.Fatal("cache blobs missing after build")
	}
	// Blobs are write-once/immutable on disk (0o444).
	for _, bb := range blobs {
		fi, err := os.Stat(filepath.Join(st.BlobsDir, bb.Name()))
		if err != nil {
			t.Fatal(err)
		}
		if fi.Mode().Perm() != 0o444 {
			t.Fatalf("blob %s mode = %o, want 0444", bb.Name(), fi.Mode().Perm())
		}
	}
}

func TestExecutedCommandsAreOnlyDeclaredFixtures(t *testing.T) {
	st := testutil.NewStore(t)
	p := testutil.InstallSharedProject(t, st)
	b := New(st)
	rep, err := b.Build(p)
	if err != nil {
		t.Fatal(err)
	}
	for _, c := range rep.CommandsRun {
		if !filepath.IsAbs(p.Name) && !contains(c, "tools/concat.sh") {
			t.Fatalf("executed non-declared command: %q", c)
		}
	}
}

func contains(s, sub string) bool {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return true
		}
	}
	return false
}
