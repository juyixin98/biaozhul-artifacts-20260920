package engine

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"cdag/internal/cache"
	"cdag/internal/graph"
	"cdag/internal/spec"
)

// testEnv bundles a workspace, an external cache and an engine.
type testEnv struct {
	t        *testing.T
	workdir  string
	cacheDir string
	store    *cache.Cache
	eng      *Engine
}

func newTestEnv(t *testing.T) *testEnv {
	t.Helper()
	root := t.TempDir()
	workdir := filepath.Join(root, "workspace")
	cacheDir := filepath.Join(root, "cache") // sibling, never inside workdir
	if err := os.MkdirAll(workdir, 0o755); err != nil {
		t.Fatal(err)
	}
	store, err := cache.Open(cacheDir)
	if err != nil {
		t.Fatal(err)
	}
	return &testEnv{
		t:        t,
		workdir:  workdir,
		cacheDir: cacheDir,
		store:    store,
		eng:      New(store, WithTimeout(30*time.Second)),
	}
}

func (e *testEnv) writeFile(rel, content string) {
	e.t.Helper()
	path := filepath.Join(e.workdir, filepath.FromSlash(rel))
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		e.t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		e.t.Fatal(err)
	}
}

func (e *testEnv) readFile(rel string) string {
	e.t.Helper()
	data, err := os.ReadFile(filepath.Join(e.workdir, filepath.FromSlash(rel)))
	if err != nil {
		e.t.Fatal(err)
	}
	return string(data)
}

func (e *testEnv) project(nodes ...*graph.Node) *spec.Project {
	return &spec.Project{ID: "test", Workdir: e.workdir, Graph: graph.Graph{Nodes: nodes}}
}

func (e *testEnv) build(nodes ...*graph.Node) *Report {
	e.t.Helper()
	rep, err := e.eng.Build(e.project(nodes...), nil)
	if err != nil {
		e.t.Fatalf("build error: %v", err)
	}
	return rep
}

func (e *testEnv) buildTargets(targets []string, nodes ...*graph.Node) *Report {
	e.t.Helper()
	rep, err := e.eng.Build(e.project(nodes...), targets)
	if err != nil {
		e.t.Fatalf("build error: %v", err)
	}
	return rep
}

func result(rep *Report, id string) *NodeResult {
	for i := range rep.Results {
		if rep.Results[i].NodeID == id {
			return &rep.Results[i]
		}
	}
	return nil
}

func requireStatus(t *testing.T, rep *Report, id, want string) *NodeResult {
	t.Helper()
	r := result(rep, id)
	if r == nil {
		t.Fatalf("node %q absent from report", id)
	}
	if r.Status != want {
		t.Fatalf("node %q status = %q, want %q (reason: %s, error: %s)", id, r.Status, want, r.Reason, r.Error)
	}
	return r
}

// mtime returns the modification time of a workspace file.
func (e *testEnv) mtime(rel string) time.Time {
	fi, err := os.Stat(filepath.Join(e.workdir, filepath.FromSlash(rel)))
	if err != nil {
		e.t.Fatal(err)
	}
	return fi.ModTime()
}

// touchOnlyTimestamps rewrites files with identical content at a distinct
// timestamp, proving time is not part of the key.
func (e *testEnv) touchOnlyTimestamps(rels ...string) {
	e.t.Helper()
	future := time.Now().Add(2 * time.Hour)
	for _, rel := range rels {
		path := filepath.Join(e.workdir, filepath.FromSlash(rel))
		data, err := os.ReadFile(path)
		if err != nil {
			e.t.Fatal(err)
		}
		if err := os.Chtimes(path, future, future); err != nil {
			e.t.Fatal(err)
		}
		_ = data
	}
}

// acceptanceGraph builds:
//
//	src (source) -> upper (tr) -> concat (cat with param) -> bundle (cat)
//	plus an unrelated side node (no dependency on the chain).
func acceptanceGraph() []*graph.Node {
	return []*graph.Node{
		{
			ID:      "src",
			Outputs: []string{"src.txt"},
		},
		{
			ID:        "upper",
			Command:   "tr a-z A-Z < src.txt > upper.txt",
			Inputs:    []string{"src.txt"},
			Outputs:   []string{"upper.txt"},
			DependsOn: []string{"src"},
			Tools:     []graph.Tool{{Name: "tr", Probe: []string{"sh", "-c", "tr --version 2>&1 | head -n 1"}}},
		},
		{
			ID:        "concat",
			Command:   "cat upper.txt > concat.txt && printf '[%s]' \"${SUFFIX}\" >> concat.txt",
			Inputs:    []string{"upper.txt"},
			Outputs:   []string{"concat.txt"},
			DependsOn: []string{"upper"},
			Params:    map[string]string{"order": "suffix"},
			Env:       []string{"SUFFIX"},
		},
		{
			ID:        "bundle",
			Command:   "cat concat.txt > bundle.txt",
			Inputs:    []string{"concat.txt"},
			Outputs:   []string{"bundle.txt"},
			DependsOn: []string{"concat"},
		},
		{
			ID:      "side",
			Command: "echo independent > side.txt",
			Outputs: []string{"side.txt"},
		},
	}
}

// TestInitialBuildAllBuilt verifies a cold build runs every command.
func TestInitialBuildAllBuilt(t *testing.T) {
	e := newTestEnv(t)
	e.writeFile("src.txt", "hello\n")
	t.Setenv("SUFFIX", "X")

	rep := e.build(acceptanceGraph()...)
	if !rep.Success {
		t.Fatalf("build failed: %+v", rep.Results)
	}
	for _, id := range []string{"src", "upper", "concat", "bundle", "side"} {
		st := StatusBuilt
		if id == "src" {
			st = StatusVerified
		}
		requireStatus(t, rep, id, st)
	}
	if got := e.readFile("upper.txt"); got != "HELLO\n" {
		t.Fatalf("upper.txt = %q", got)
	}
	if got := e.readFile("concat.txt"); got != "HELLO\n[X]" {
		t.Fatalf("concat.txt = %q", got)
	}
	if got := e.readFile("bundle.txt"); got != "HELLO\n[X]" {
		t.Fatalf("bundle.txt = %q", got)
	}
}

// TestNoOpRebuild verifies a second build with no changes is a full cache hit.
func TestNoOpRebuild(t *testing.T) {
	e := newTestEnv(t)
	e.writeFile("src.txt", "hello\n")
	t.Setenv("SUFFIX", "X")

	nodes := acceptanceGraph()
	e.build(nodes...)
	rep := e.build(nodes...)
	if !rep.Success {
		t.Fatalf("second build failed")
	}
	for _, id := range []string{"upper", "concat", "bundle", "side"} {
		r := requireStatus(t, rep, id, StatusCached)
		if !r.Cached {
			t.Fatalf("node %q not marked cached", id)
		}
		if !strings.Contains(r.Reason, "cache hit") {
			t.Fatalf("node %q reason should explain hit: %q", id, r.Reason)
		}
	}
	requireStatus(t, rep, "src", StatusVerified)
	if rep.Counts.Built != 0 {
		t.Fatalf("expected zero rebuilt nodes, got %d", rep.Counts.Built)
	}
}

// TestTimestampOnlyChange verifies content-addressed, not timestamp-addressed:
// touching files with identical content must not rebuild anything.
func TestTimestampOnlyChange(t *testing.T) {
	e := newTestEnv(t)
	e.writeFile("src.txt", "hello\n")
	t.Setenv("SUFFIX", "X")
	nodes := acceptanceGraph()
	e.build(nodes...)

	beforeUpper := e.mtime("upper.txt")
	e.touchOnlyTimestamps("src.txt", "upper.txt", "concat.txt")

	rep := e.build(nodes...)
	for _, id := range []string{"upper", "concat", "bundle", "side"} {
		requireStatus(t, rep, id, StatusCached)
	}
	// Restored cached outputs keep workspace state; the key assertion is
	// status cached even though src.txt mtime changed.
	_ = beforeUpper
}

// TestTransitiveChange verifies editing a leaf input rebuilds exactly its
// transitive dependents, while an unrelated node is reused.
func TestTransitiveChange(t *testing.T) {
	e := newTestEnv(t)
	e.writeFile("src.txt", "hello\n")
	t.Setenv("SUFFIX", "X")
	nodes := acceptanceGraph()
	e.build(nodes...)

	// Change source content WITHOUT controlling timestamps: write new bytes.
	time.Sleep(10 * time.Millisecond)
	e.writeFile("src.txt", "world\n")
	rep := e.build(nodes...)

	requireStatus(t, rep, "src", StatusVerified)
	upper := requireStatus(t, rep, "upper", StatusBuilt)
	if !explainsContentChange(upper) {
		t.Fatalf("upper should explain input/dependency content change: %+v", upper.Changes)
	}
	concat := requireStatus(t, rep, "concat", StatusBuilt)
	if !containsChange(concat, "dependency_changed") && !containsChange(concat, "output_changed") {
		t.Fatalf("concat should explain transitive dependency change: %+v", concat.Changes)
	}
	bundle := requireStatus(t, rep, "bundle", StatusBuilt)
	if !containsChange(bundle, "dependency_changed") && !containsChange(bundle, "output_changed") {
		t.Fatalf("bundle should explain transitive dependency change: %+v", bundle.Changes)
	}
	// Unrelated node must be reused from cache.
	requireStatus(t, rep, "side", StatusCached)

	if got := e.readFile("upper.txt"); got != "WORLD\n" {
		t.Fatalf("upper.txt = %q, want WORLD", got)
	}
	if got := e.readFile("bundle.txt"); got != "WORLD\n[X]" {
		t.Fatalf("bundle.txt = %q", got)
	}
}

// TestParameterChange verifies a param change rebuilds only that node and
// its dependents, and the explanation names the parameter.
func TestParameterChange(t *testing.T) {
	e := newTestEnv(t)
	e.writeFile("src.txt", "hello\n")
	t.Setenv("SUFFIX", "X")
	nodes := acceptanceGraph()
	e.build(nodes...)

	nodes2 := acceptanceGraph()
	for _, n := range nodes2 {
		if n.ID == "concat" {
			n.Params = map[string]string{"order": "suffix", "mode": "bracketed"}
		}
	}
	rep := e.build(nodes2...)
	requireStatus(t, rep, "upper", StatusCached)
	concat := requireStatus(t, rep, "concat", StatusBuilt)
	if !containsChange(concat, "param_added") {
		t.Fatalf("concat should explain param add: %+v", concat.Changes)
	}
	requireStatus(t, rep, "bundle", StatusBuilt)
	requireStatus(t, rep, "side", StatusCached)
}

// TestEnvChange verifies declared environment variables feed the key.
func TestEnvChange(t *testing.T) {
	e := newTestEnv(t)
	e.writeFile("src.txt", "hello\n")
	t.Setenv("SUFFIX", "X")
	nodes := acceptanceGraph()
	e.build(nodes...)

	t.Setenv("SUFFIX", "Z")
	rep := e.build(nodes...)
	requireStatus(t, rep, "upper", StatusCached)
	concat := requireStatus(t, rep, "concat", StatusBuilt)
	if !containsChange(concat, "env_changed") {
		t.Fatalf("concat should explain env change: %+v", concat.Changes)
	}
	if got := e.readFile("concat.txt"); got != "HELLO\n[Z]" {
		t.Fatalf("concat.txt = %q", got)
	}
	requireStatus(t, rep, "bundle", StatusBuilt)
}

// TestUndeclaredEnvIsolated verifies only declared env vars reach commands.
func TestUndeclaredEnvIsolated(t *testing.T) {
	e := newTestEnv(t)
	nodes := []*graph.Node{{
		ID:      "leak",
		Command: "printf '%s' \"${SECRET:-<absent>}\" > leak.txt",
		Outputs: []string{"leak.txt"},
		// SECRET deliberately not declared in Env.
	}}
	t.Setenv("SECRET", "topsecret")
	rep := e.build(nodes...)
	requireStatus(t, rep, "leak", StatusBuilt)
	if got := e.readFile("leak.txt"); got != "<absent>" {
		t.Fatalf("undeclared env leaked into command: %q", got)
	}
}

// TestToolVersionChange verifies the tool-version dimension of the key.
// It does so via two nodes that declare different probed "versions" using
// a trivial echo probe, so the test is hermetic and needs no real tool.
func TestToolVersionChange(t *testing.T) {
	e := newTestEnv(t)
	mk := func(version string) []*graph.Node {
		return []*graph.Node{{
			ID:      "t",
			Command: "echo out > out.txt",
			Outputs: []string{"out.txt"},
			Tools:   []graph.Tool{{Name: "fake", Probe: []string{"sh", "-c", "echo " + version}}},
		}}
	}
	e.build(mk("v1")...)
	rep := e.build(mk("v2")...)
	r := requireStatus(t, rep, "t", StatusBuilt)
	if !containsChange(r, "tool_version_changed") {
		t.Fatalf("should explain tool version change: %+v", r.Changes)
	}
}

// TestFailedNodeNoPublish verifies a failing command does not publish a
// cache entry, its dependents are blocked, and a retry with fixed inputs
// rebuilds and then caches.
func TestFailedNodeNoPublish(t *testing.T) {
	e := newTestEnv(t)
	e.writeFile("src.txt", "hello\n")
	nodes := []*graph.Node{
		{ID: "src", Outputs: []string{"src.txt"}},
		{
			ID:        "mayfail",
			Command:   "if [ -f go.txt ]; then tr a-z A-Z < src.txt > out.txt; else echo 'missing go.txt' >&2; exit 3; fi",
			Inputs:    []string{"src.txt"},
			Outputs:   []string{"out.txt"},
			DependsOn: []string{"src"},
		},
		{
			ID:        "down",
			Command:   "cat out.txt > down.txt",
			Inputs:    []string{"out.txt"},
			Outputs:   []string{"down.txt"},
			DependsOn: []string{"mayfail"},
		},
	}
	rep := e.build(nodes...)
	fail := requireStatus(t, rep, "mayfail", StatusFailed)
	if fail.ExitCode == nil || *fail.ExitCode != 3 {
		t.Fatalf("want exit code 3, got %v", fail.ExitCode)
	}
	requireStatus(t, rep, "down", StatusBlocked)
	// Nothing should be cached under the failed key.
	if e.store.Has(fail.Key) {
		t.Fatal("failed node must not publish a cache entry")
	}
	if _, err := os.Stat(filepath.Join(e.workdir, "out.txt")); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("failed node must not leave declared output, got err=%v", err)
	}

	// Fix inputs: node should rebuild (previous attempt failed) and succeed.
	e.writeFile("go.txt", "present\n")
	rep = e.build(nodes...)
	requireStatus(t, rep, "mayfail", StatusBuilt)
	requireStatus(t, rep, "down", StatusBuilt)
	if got := e.readFile("down.txt"); got != "HELLO\n" {
		t.Fatalf("down.txt = %q", got)
	}
	// Now it must be cached.
	rep = e.build(nodes...)
	requireStatus(t, rep, "mayfail", StatusCached)
	requireStatus(t, rep, "down", StatusCached)
}

// TestMissingInputFails verifies a declared-but-absent input fails the node
// without running its command.
func TestMissingInputFails(t *testing.T) {
	e := newTestEnv(t)
	nodes := []*graph.Node{{
		ID:      "n",
		Command: "echo should-not-run > out.txt",
		Inputs:  []string{"nope.txt"},
		Outputs: []string{"out.txt"},
	}}
	rep := e.build(nodes...)
	r := requireStatus(t, rep, "n", StatusFailed)
	if !strings.Contains(r.Error, "missing or unreadable input") {
		t.Fatalf("unexpected error: %q", r.Error)
	}
	if _, err := os.Stat(filepath.Join(e.workdir, "out.txt")); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("command must not have run when input missing")
	}
}

// TestCycleRejected verifies the engine rejects a cycle before executing.
func TestCycleRejected(t *testing.T) {
	e := newTestEnv(t)
	nodes := []*graph.Node{
		{ID: "a", Command: "echo a > a.txt", Outputs: []string{"a.txt"}, DependsOn: []string{"b"}},
		{ID: "b", Command: "echo b > b.txt", Outputs: []string{"b.txt"}, DependsOn: []string{"a"}},
	}
	_, err := e.eng.Build(e.project(nodes...), nil)
	if err == nil {
		t.Fatal("expected cycle error")
	}
	if !strings.Contains(err.Error(), "cycle") {
		t.Fatalf("error should mention cycle: %v", err)
	}
	for _, f := range []string{"a.txt", "b.txt"} {
		if _, err := os.Stat(filepath.Join(e.workdir, f)); !errors.Is(err, os.ErrNotExist) {
			t.Fatalf("no command may run on a cyclic graph: %s exists", f)
		}
	}
}

// TestTargetClosure verifies building one target reuses/builds only its closure.
func TestTargetClosure(t *testing.T) {
	e := newTestEnv(t)
	e.writeFile("src.txt", "hello\n")
	t.Setenv("SUFFIX", "X")
	nodes := acceptanceGraph()
	// Build only "upper": side and the concat chain are out of closure.
	rep := e.buildTargets([]string{"upper"}, nodes...)
	requireStatus(t, rep, "upper", StatusBuilt)
	if result(rep, "side") != nil {
		t.Fatal("side must be excluded from upper's closure")
	}
	if result(rep, "concat") != nil {
		t.Fatal("concat must be excluded from upper's closure")
	}
}

// TestCacheSeparateFromWorkdir asserts the cache stores entries outside the
// workspace and workdir contains no cache artifacts.
func TestCacheSeparateFromWorkdir(t *testing.T) {
	e := newTestEnv(t)
	e.writeFile("src.txt", "hello\n")
	nodes := []*graph.Node{
		{ID: "src", Outputs: []string{"src.txt"}},
		{ID: "up", Command: "tr a-z A-Z < src.txt > up.txt", Inputs: []string{"src.txt"}, Outputs: []string{"up.txt"}, DependsOn: []string{"src"}},
	}
	e.build(nodes...)
	entries, err := os.ReadDir(e.workdir)
	if err != nil {
		t.Fatal(err)
	}
	for _, en := range entries {
		if strings.Contains(en.Name(), "cdag") || strings.Contains(en.Name(), "entries") || strings.Contains(en.Name(), "history") {
			t.Fatalf("cache artifact leaked into workdir: %s", en.Name())
		}
	}
	if _, err := os.Stat(filepath.Join(e.cacheDir, "entries")); err != nil {
		t.Fatalf("cache entries dir missing: %v", err)
	}
}

// TestReportJSON verifies the report serializes the key explanatory fields.
func TestReportJSON(t *testing.T) {
	e := newTestEnv(t)
	e.writeFile("src.txt", "hello\n")
	t.Setenv("SUFFIX", "X")
	nodes := acceptanceGraph()
	e.build(nodes...)
	// Change input so the second build's rebuilt nodes carry "changes".
	time.Sleep(10 * time.Millisecond)
	e.writeFile("src.txt", "again\n")
	rep := e.build(nodes...)
	data, err := json.Marshal(rep)
	if err != nil {
		t.Fatal(err)
	}
	s := string(data)
	for _, want := range []string{"\"success\":true", "\"cache_dir\"", "\"changes\"", "\"reason\"", "\"key\""} {
		if !strings.Contains(s, want) {
			t.Fatalf("report JSON missing %s in %s", want, s)
		}
	}
}

func containsChange(r *NodeResult, typ string) bool {
	for _, c := range r.Changes {
		if c.Type == typ {
			return true
		}
	}
	return false
}

// explainsContentChange is true when a node's miss explanation names a
// content change, whether through its own input or a dependency output.
func explainsContentChange(r *NodeResult) bool {
	for _, typ := range []string{"input_changed", "output_changed", "dependency_changed"} {
		if containsChange(r, typ) {
			return true
		}
	}
	return false
}
