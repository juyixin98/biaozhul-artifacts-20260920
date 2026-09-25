package service

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// writeFiles creates root-relative files with the given contents.
func writeFiles(t *testing.T, root string, files map[string]string) {
	t.Helper()
	for rel, content := range files {
		p := filepath.Join(root, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
	}
}

func newService(t *testing.T, root string) *Service {
	t.Helper()
	svc, err := NewService(filepath.Join(t.TempDir(), "cache"))
	if err != nil {
		t.Fatal(err)
	}
	// Sanity: the temp cache dir must never overlap the fixture root.
	if strings.HasPrefix(svc.CacheDir, root) {
		t.Fatalf("cache %q overlaps source root %q", svc.CacheDir, root)
	}
	return svc
}

// TestFullScanSameNameHeaders verifies that quoted includes prefer the
// including file's directory while angled includes use include dirs, even
// when several headers share the same basename.
func TestFullScanSameNameHeaders(t *testing.T) {
	root := filepath.Join(t.TempDir(), "proj")
	writeFiles(t, root, map[string]string{
		"util.h":     "#ifndef TOP_UTIL\n#define TOP_UTIL\n#endif\n",
		"sub/util.h": "/* sub util */\n",
		"inc/util.h": "// inc util\n",
		"main.c":     "#include \"util.h\"\n#include <util.h>\n",
		"sub/b.c":    "#include \"util.h\"\n",
	})
	svc := newService(t, root)
	cfg := Config{SourceRoot: root, IncludeDirs: []string{"inc"}}

	res, err := svc.FullScan(cfg)
	if err != nil {
		t.Fatalf("FullScan: %v", err)
	}
	if got := res.Graph["main.c"]; len(got) != 2 || got[0] != "inc/util.h" || got[1] != "util.h" {
		t.Errorf("main.c deps = %v, want [inc/util.h util.h]", got)
	}
	if got := res.Graph["sub/b.c"]; len(got) != 1 || got[0] != "sub/util.h" {
		t.Errorf("sub/b.c deps = %v, want [sub/util.h] (quoted, same dir wins)", got)
	}
}

// TestFullScanNestedPaths covers nested include paths and transitive deps.
func TestFullScanNestedPaths(t *testing.T) {
	root := filepath.Join(t.TempDir(), "proj")
	writeFiles(t, root, map[string]string{
		"main.c":           "#include \"lib/api/client.h\"\n",
		"lib/api/client.h": "#include \"common.h\"\n#include \"../util/buf.h\"\n",
		"lib/api/common.h": "// comment-only header\n",
		"lib/util/buf.h":   "#include \"common.h\"\n",
	})
	svc := newService(t, root)

	res, err := svc.FullScan(Config{SourceRoot: root, IncludeDirs: []string{"lib/api"}})
	if err != nil {
		t.Fatalf("FullScan: %v", err)
	}
	if got := res.Graph["main.c"]; len(got) != 1 || got[0] != "lib/api/client.h" {
		t.Errorf("main.c deps = %v, want [lib/api/client.h]", got)
	}
	if got := res.Graph["lib/api/client.h"]; len(got) != 2 || got[0] != "lib/api/common.h" || got[1] != "lib/util/buf.h" {
		t.Errorf("client.h deps = %v, want [lib/api/common.h lib/util/buf.h]", got)
	}
	if got := res.AffectedFiles; len(got) != 4 {
		t.Errorf("full scan affected files = %v, want all 4 files", got)
	}
}

// TestPseudoIncludesInComments ensures fake includes in comments and
// strings are neither followed nor reported unresolved.
func TestPseudoIncludesInComments(t *testing.T) {
	root := filepath.Join(t.TempDir(), "proj")
	writeFiles(t, root, map[string]string{
		"a.c": `// #include "line-fake.h"
/* #include "block-fake.h" */
/*
 * #include <multi-fake.h>
 */
const char *s = "#include <string-fake.h>";
#include "real.h"
`,
		"real.h": "// real\n",
	})
	svc := newService(t, root)

	res, err := svc.FullScan(Config{SourceRoot: root})
	if err != nil {
		t.Fatalf("FullScan: %v", err)
	}
	if got := res.Graph["a.c"]; len(got) != 1 || got[0] != "real.h" {
		t.Errorf("a.c deps = %v, want [real.h]", got)
	}
	if len(res.Unresolved) != 0 {
		t.Errorf("unexpected unresolved includes: %+v", res.Unresolved)
	}
}

// TestMacroIncludeReported ensures macro-generated includes fail loudly.
func TestMacroIncludeReported(t *testing.T) {
	root := filepath.Join(t.TempDir(), "proj")
	writeFiles(t, root, map[string]string{
		"a.c":    "#include \"real.h\"\n#include GENERATED_HEADER\n",
		"real.h": "",
	})
	svc := newService(t, root)

	_, err := svc.FullScan(Config{SourceRoot: root})
	if err == nil {
		t.Fatal("expected error for macro-generated include")
	}
	if !strings.Contains(err.Error(), "macro-generated") || !strings.Contains(err.Error(), "a.c") {
		t.Errorf("error %q should name the file and mention macro-generated", err.Error())
	}
}

// TestCircularIncludesDoesNotHang verifies cycles are tolerated and
// reported with deterministic output.
func TestCircularIncludesDoesNotHang(t *testing.T) {
	root := filepath.Join(t.TempDir(), "proj")
	writeFiles(t, root, map[string]string{
		"a.h": "#ifndef A_H\n#define A_H\n#include \"b.h\"\n#endif\n",
		"b.h": "#ifndef B_H\n#define B_H\n#include \"a.h\"\n#endif\n",
		"c.h": "#include \"a.h\"\n",
	})
	svc := newService(t, root)

	res, err := svc.FullScan(Config{SourceRoot: root})
	if err != nil {
		t.Fatalf("FullScan: %v", err)
	}
	if len(res.Cycles) != 1 || len(res.Cycles[0]) != 2 ||
		res.Cycles[0][0] != "a.h" || res.Cycles[0][1] != "b.h" {
		t.Errorf("cycles = %v, want [[a.h b.h]]", res.Cycles)
	}
	if got := res.Graph["c.h"]; len(got) != 1 || got[0] != "a.h" {
		t.Errorf("c.h deps = %v, want [a.h]", got)
	}
}

// TestIncrementalChangedFile recomputes affected files/targets after an edit.
func TestIncrementalChangedFile(t *testing.T) {
	root := filepath.Join(t.TempDir(), "proj")
	writeFiles(t, root, map[string]string{
		"base.h":      "#define BASE 1\n",
		"leaf.h":      "#include \"base.h\"\n",
		"main.c":      "#include \"leaf.h\"\n",
		"other.c":     "#include \"unrelated.h\"\n",
		"unrelated.h": "",
	})
	svc := newService(t, root)
	cfg := Config{
		SourceRoot: root,
		Targets: []Target{
			{Name: "app", Sources: []string{"main.c"}},
			{Name: "tool", Sources: []string{"other.c"}},
		},
	}
	if _, err := svc.FullScan(cfg); err != nil {
		t.Fatalf("FullScan: %v", err)
	}

	// Edit base.h: main.c -> leaf.h -> base.h must be flagged, other.c not.
	res, err := svc.Incremental(cfg, []string{"base.h"}, nil)
	if err != nil {
		t.Fatalf("Incremental: %v", err)
	}
	wantFiles := []string{"base.h", "leaf.h", "main.c"}
	if got := strings.Join(res.AffectedFiles, ","); got != strings.Join(wantFiles, ",") {
		t.Errorf("affected files = %v, want %v", res.AffectedFiles, wantFiles)
	}
	wantTargets := []string{"app"}
	if got := strings.Join(res.AffectedTargets, ","); got != strings.Join(wantTargets, ",") {
		t.Errorf("affected targets = %v, want %v", res.AffectedTargets, wantTargets)
	}
	if res.FilesScanned != 1 {
		t.Errorf("filesScanned = %d, want 1", res.FilesScanned)
	}
}

// TestIncrementalDeletedFile removes a header and verifies the node and its
// inbound edges disappear, while dependents are reported affected.
func TestIncrementalDeletedFile(t *testing.T) {
	root := filepath.Join(t.TempDir(), "proj")
	writeFiles(t, root, map[string]string{
		"victim.h": "// will be deleted\n",
		"keep.h":   "// kept\n",
		"main.c":   "#include \"victim.h\"\n#include \"keep.h\"\n",
	})
	svc := newService(t, root)
	cfg := Config{SourceRoot: root, Targets: []Target{{Name: "app", Sources: []string{"main.c"}}}}
	if _, err := svc.FullScan(cfg); err != nil {
		t.Fatalf("FullScan: %v", err)
	}

	if err := os.Remove(filepath.Join(root, "victim.h")); err != nil {
		t.Fatal(err)
	}
	res, err := svc.Incremental(cfg, nil, []string{"victim.h"})
	if err != nil {
		t.Fatalf("Incremental: %v", err)
	}
	if _, ok := res.Graph["victim.h"]; ok {
		t.Error("deleted node victim.h should be gone from graph")
	}
	if got := res.Graph["main.c"]; len(got) != 1 || got[0] != "keep.h" {
		t.Errorf("main.c deps = %v, want [keep.h] (inbound edge to victim removed)", got)
	}
	affected := strings.Join(res.AffectedFiles, ",")
	if affected != "main.c,victim.h" {
		t.Errorf("affected = %q, want main.c,victim.h", affected)
	}
	if len(res.AffectedTargets) != 1 || res.AffectedTargets[0] != "app" {
		t.Errorf("affected targets = %v, want [app]", res.AffectedTargets)
	}

	// Cache survives in the separate cache directory after the deletion.
	g, err := svc.LoadCache(root)
	if err != nil || g == nil {
		t.Fatalf("LoadCache: %v", err)
	}
	if _, ok := g.Deps["victim.h"]; ok {
		t.Error("cache should no longer contain victim.h")
	}
}

// TestIncrementalRequiresFullScan checks the documented first-scan error.
func TestIncrementalRequiresFullScan(t *testing.T) {
	root := filepath.Join(t.TempDir(), "proj")
	writeFiles(t, root, map[string]string{"a.c": ""})
	svc := newService(t, root)

	_, err := svc.Incremental(Config{SourceRoot: root}, []string{"a.c"}, nil)
	if err == nil || !strings.Contains(err.Error(), "full scan") {
		t.Errorf("err = %v, want guidance to run full scan first", err)
	}
}

// TestCacheInsideRootRejected enforces cache/working-directory separation.
func TestCacheInsideRootRejected(t *testing.T) {
	root := filepath.Join(t.TempDir(), "proj")
	writeFiles(t, root, map[string]string{"a.c": ""})

	bad, err := NewService(filepath.Join(root, ".cache"))
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	if _, err := bad.FullScan(Config{SourceRoot: root}); err == nil ||
		!strings.Contains(err.Error(), "separate") {
		t.Errorf("err = %v, want cache-separation error", err)
	}
}

// TestUnresolvedIncludeReported covers includes that match no file.
func TestUnresolvedIncludeReported(t *testing.T) {
	root := filepath.Join(t.TempDir(), "proj")
	writeFiles(t, root, map[string]string{
		"a.c":      "#include \"exists.h\"\n#include <missing.h>\n",
		"exists.h": "",
	})
	svc := newService(t, root)

	res, err := svc.FullScan(Config{SourceRoot: root})
	if err != nil {
		t.Fatalf("FullScan: %v", err)
	}
	if len(res.Unresolved) != 1 {
		t.Fatalf("unresolved = %+v, want 1 entry", res.Unresolved)
	}
	u := res.Unresolved[0]
	if u.File != "a.c" || u.Include != "missing.h" || !u.Angle || u.Line != 2 {
		t.Errorf("unresolved entry = %+v", u)
	}
}
