package builder

import (
	"path/filepath"
	"sort"
	"testing"
	"time"
)

type memStore map[string]StoredFile

type mapStore struct{ m memStore }

func newMapStore() *mapStore                        { return &mapStore{m: memStore{}} }
func (s *mapStore) Get(p string) (StoredFile, bool) { f, ok := s.m[p]; return f, ok }
func (s *mapStore) Put(f StoredFile)                { s.m[f.Path] = f }
func (s *mapStore) len() int                        { return len(s.m) }

func writeTree(t *testing.T, root string, files map[string]string) {
	t.Helper()
	for rel, content := range files {
		p := filepath.Join(root, filepath.FromSlash(rel))
		if err := writeFileMkdir(p, []byte(content)); err != nil {
			t.Fatal(err)
		}
	}
}

func writeFileMkdir(path string, data []byte) error {
	return writeFile(path, data)
}

func buildFor(t *testing.T, root string, targets []string, quoteDirs, sysDirs []string, store Store) *Result {
	t.Helper()
	res, err := Build(Options{Root: root, Targets: targets, QuoteDirs: quoteDirs, SystemDirs: sysDirs}, store)
	if err != nil {
		t.Fatalf("Build: %v", err)
	}
	return res
}

func nodeSet(res *Result) map[string]string {
	m := map[string]string{}
	for _, n := range res.Nodes {
		m[n.Path] = n.Kind
	}
	return m
}

func edgeSet(res *Result) map[[2]string]bool {
	m := map[[2]string]bool{}
	for _, e := range res.Edges {
		m[[2]string{e.From, e.To}] = true
	}
	return m
}

func TestBuild_SameNameHeadersDistinguished(t *testing.T) {
	root := t.TempDir()
	sysdir := filepath.Join(t.TempDir(), "sys") // 系统目录位于项目根之外
	writeTree(t, root, map[string]string{
		"app/main.c": "#include \"name.h\"\n#include <net/name.h>\n",
		"app/name.h": "#define APP 1\n",
	})
	if err := writeFile(filepath.Join(sysdir, "net/name.h"), []byte("#define NET 1\n")); err != nil {
		t.Fatal(err)
	}
	res := buildFor(t, root, []string{"app/main.c"}, nil, []string{sysdir}, nil)

	if res.Stats.ErrorsTotal != 0 {
		t.Fatalf("unexpected errors: %+v", res.Diagnostics)
	}
	nodes := nodeSet(res)
	if _, ok := nodes["app/name.h"]; !ok {
		t.Errorf("missing app/name.h: %v", nodes)
	}
	sysNode := filepath.ToSlash(filepath.Join(sysdir, "net/name.h"))
	if _, ok := nodes[sysNode]; !ok {
		t.Errorf("missing external %s: %v", sysNode, nodes)
	}
	if nodes[sysNode] != "external" {
		t.Errorf("sys header kind = %s, want external", nodes[sysNode])
	}
}

func TestBuild_SameNameHeaderUnderRootIsInternal(t *testing.T) {
	// 搜索目录即便位于 root 内，节点也按“在根内”归为 internal。
	root := t.TempDir()
	writeTree(t, root, map[string]string{
		"app/main.c": "#include <name.h>\n",
		"sys/name.h": "#define S 1\n",
	})
	res := buildFor(t, root, []string{"app/main.c"}, nil, []string{filepath.Join(root, "sys")}, nil)
	if got := nodeSet(res)["sys/name.h"]; got != "internal" {
		t.Errorf("kind = %q, want internal", got)
	}
}

func TestBuild_CycleDetected(t *testing.T) {
	root := t.TempDir()
	writeTree(t, root, map[string]string{
		"m.c": "#include \"a.h\"\n",
		"a.h": "#include \"b.h\"\n",
		"b.h": "#include \"a.h\"\n",
	})
	res := buildFor(t, root, []string{"m.c"}, nil, nil, nil)

	if res.Stats.CyclesTotal != 1 {
		t.Fatalf("cycles = %d, want 1 (%v)", res.Stats.CyclesTotal, res.Cycles)
	}
	want := []string{"a.h", "b.h", "a.h"}
	if len(res.Cycles[0]) != len(want) {
		t.Fatalf("cycle = %v, want %v", res.Cycles[0], want)
	}
	for i := range want {
		if res.Cycles[0][i] != want[i] {
			t.Fatalf("cycle = %v, want %v", res.Cycles[0], want)
		}
	}
	// 三个真实文件都应在图中（没有无限递归）。
	nodes := nodeSet(res)
	for _, p := range []string{"m.c", "a.h", "b.h"} {
		if _, ok := nodes[p]; !ok {
			t.Errorf("node %s missing", p)
		}
	}
}

func TestBuild_SelfCycle(t *testing.T) {
	root := t.TempDir()
	writeTree(t, root, map[string]string{
		"m.c": "#include \"a.h\"\n",
		"a.h": "#include \"a.h\"\n",
	})
	res := buildFor(t, root, []string{"m.c"}, nil, nil, nil)
	if res.Stats.CyclesTotal != 1 {
		t.Fatalf("cycles = %d, want 1: %v", res.Stats.CyclesTotal, res.Cycles)
	}
	want := []string{"a.h", "a.h"}
	if len(res.Cycles[0]) != 2 || res.Cycles[0][0] != want[0] || res.Cycles[0][1] != want[1] {
		t.Fatalf("cycle = %v, want %v", res.Cycles[0], want)
	}
}

func TestBuild_CommentsPseudoAndMissing(t *testing.T) {
	root := t.TempDir()
	writeTree(t, root, map[string]string{
		"m.c": `// #include "fake.h"
/* #include "fake2.h" */
#include "real.h"
#include "gone.h"
`,
		"real.h": "#define R 1\n",
	})
	res := buildFor(t, root, []string{"m.c"}, nil, nil, nil)

	nodes := nodeSet(res)
	if _, ok := nodes["fake.h"]; ok {
		t.Errorf("fake.h must not appear: %v", nodes)
	}
	if _, ok := nodes["fake2.h"]; ok {
		t.Errorf("fake2.h must not appear: %v", nodes)
	}
	if _, ok := nodes["gone.h"]; !ok {
		t.Errorf("gone.h should be a missing node: %v", nodes)
	}
	if nodes["gone.h"] != "missing" {
		t.Errorf("gone.h kind = %s, want missing", nodes["gone.h"])
	}
	if res.Stats.WarningsTotal < 1 {
		t.Errorf("expected warning for unresolved include, got %+v", res.Diagnostics)
	}
}

func TestBuild_MacroIncludeFailsStatus(t *testing.T) {
	root := t.TempDir()
	writeTree(t, root, map[string]string{
		"m.c": "#define X \"y.h\"\n#include X\n",
	})
	res := buildFor(t, root, []string{"m.c"}, nil, nil, nil)
	if res.Stats.ErrorsTotal != 1 {
		t.Fatalf("errors = %d, want 1: %+v", res.Stats.ErrorsTotal, res.Diagnostics)
	}
}

func TestBuild_StableOutputAcrossRuns(t *testing.T) {
	root := t.TempDir()
	writeTree(t, root, map[string]string{
		"m.c": "#include \"z.h\"\n#include \"a.h\"\n",
		"z.h": "#include \"a.h\"\n",
		"a.h": "#define A 1\n",
	})
	r1 := buildFor(t, root, []string{"m.c"}, nil, nil, nil)
	time.Sleep(2 * time.Millisecond)
	r2 := buildFor(t, root, []string{"m.c"}, nil, nil, nil)

	if !sortedNodesEqual(r1.Nodes, r2.Nodes) {
		t.Fatal("node order/content unstable across runs")
	}
	if !edgesEqual(r1.Edges, r2.Edges) {
		t.Fatal("edge order/content unstable across runs")
	}
	// 节点必须按路径排序
	if !sort.SliceIsSorted(r1.Nodes, func(i, j int) bool {
		return r1.Nodes[i].Path < r1.Nodes[j].Path
	}) {
		t.Errorf("nodes not sorted: %v", r1.Nodes)
	}
}

func TestBuild_CacheReuseOnUnchanged(t *testing.T) {
	root := t.TempDir()
	writeTree(t, root, map[string]string{
		"m.c": "#include \"a.h\"\n",
		"a.h": "#include \"b.h\"\n",
		"b.h": "#define B 1\n",
	})
	store := newMapStore()
	r1 := buildFor(t, root, []string{"m.c"}, nil, nil, store)
	if r1.Stats.CacheHits != 0 || r1.Stats.FilesRead != 3 {
		t.Fatalf("first build: hits=%d read=%d", r1.Stats.CacheHits, r1.Stats.FilesRead)
	}
	if store.len() != 3 {
		t.Fatalf("store should hold 3 files, got %d", store.len())
	}
	r2 := buildFor(t, root, []string{"m.c"}, nil, nil, store)
	if r2.Stats.FilesRead != 0 || r2.Stats.CacheHits != 3 {
		t.Fatalf("second build: read=%d hits=%d, want 0/3", r2.Stats.FilesRead, r2.Stats.CacheHits)
	}
}

func TestBuild_DeletedFileBecomesMissing(t *testing.T) {
	root := t.TempDir()
	files := map[string]string{
		"m.c": "#include \"a.h\"\n",
		"a.h": "#define A 1\n",
	}
	writeTree(t, root, files)
	r1 := buildFor(t, root, []string{"m.c"}, nil, nil, nil)
	if r1.Stats.MissingFiles != 0 {
		t.Fatalf("unexpected missing: %v", r1.Nodes)
	}
	if err := removeFile(filepath.Join(root, "a.h")); err != nil {
		t.Fatal(err)
	}
	r2 := buildFor(t, root, []string{"m.c"}, nil, nil, nil)
	if r2.Stats.MissingFiles != 1 {
		t.Fatalf("missing = %d, want 1: %v", r2.Stats.MissingFiles, r2.Nodes)
	}
	if nodeSet(r2)["a.h"] != "missing" {
		t.Errorf("a.h should be missing")
	}
}

func TestBuild_MissingNestedIncludeKeyNotDoubled(t *testing.T) {
	// src/h/x.h 中以引号包含 "sub/y.h"，首个候选应为 src/h/sub/y.h，
	// 不能因为逻辑路径已含 sub/ 而重复拼成 src/h/sub/sub/y.h。
	root := t.TempDir()
	writeTree(t, root, map[string]string{
		"src/m.c":   "#include \"h/x.h\"\n",
		"src/h/x.h": "#include \"sub/y.h\"\n",
	})
	res := buildFor(t, root, []string{"src/m.c"}, nil, nil, nil)
	if _, ok := nodeSet(res)["src/h/sub/y.h"]; !ok {
		t.Fatalf("expected missing node src/h/sub/y.h, got %v", nodeSet(res))
	}
	if _, bad := nodeSet(res)["src/h/sub/sub/y.h"]; bad {
		t.Fatalf("missing node key must not double path segment")
	}
}

func TestBuild_TargetValidation(t *testing.T) {
	if _, err := Build(Options{Root: t.TempDir(), Targets: nil}, nil); err == nil {
		t.Error("empty targets should fail")
	}
	root := t.TempDir()
	if _, err := Build(Options{Root: root, Targets: []string{"../escape.c"}}, nil); err == nil {
		t.Error("escaping target should fail")
	}
	if _, err := Build(Options{Root: root, Targets: []string{"abs/present.c"}}, nil); err == nil {
		t.Error("nonexistent target should fail")
	}
}

func TestBuild_DiamondIncludesDedupEdges(t *testing.T) {
	root := t.TempDir()
	writeTree(t, root, map[string]string{
		"m.c": "#include \"a.h\"\n#include \"b.h\"\n",
		"a.h": "#include \"c.h\"\n",
		"b.h": "#include \"c.h\"\n",
		"c.h": "#define C 1\n",
	})
	res := buildFor(t, root, []string{"m.c"}, nil, nil, nil)
	if res.Stats.EdgesTotal != 4 {
		t.Fatalf("edges = %d, want 4: %+v", res.Stats.EdgesTotal, res.Edges)
	}
	if res.Stats.CyclesTotal != 0 {
		t.Errorf("diamond is not a cycle, got %v", res.Cycles)
	}
}

func sortedNodesEqual(a, b []Node) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		// 忽略 mtime（秒级精度可能变化）
		if a[i].Path != b[i].Path || a[i].Kind != b[i].Kind ||
			a[i].Missing != b[i].Missing || a[i].Size != b[i].Size {
			return false
		}
	}
	return true
}

func edgesEqual(a, b []Edge) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
