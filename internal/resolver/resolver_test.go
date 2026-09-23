package resolver

import (
	"os"
	"path/filepath"
	"testing"

	"depscanner/internal/scanner"
)

func setupResolverTree(t *testing.T) (root string, r *Resolver) {
	t.Helper()
	root = t.TempDir()
	mustMkdir(t, filepath.Join(root, "app"))
	mustMkdir(t, filepath.Join(root, "app/util"))
	mustMkdir(t, filepath.Join(root, "net"))
	mustMkdir(t, filepath.Join(root, "sys"))
	mustWrite(t, filepath.Join(root, "app/name.h"), "")
	mustWrite(t, filepath.Join(root, "net/name.h"), "")
	mustWrite(t, filepath.Join(root, "app/util/u.h"), "")
	mustWrite(t, filepath.Join(root, "sys/stdio.h"), "")

	r, err := New(root, []string{"net"}, []string{"sys"})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	return root, r
}

func mustMkdir(t *testing.T, p string) {
	t.Helper()
	if err := os.MkdirAll(p, 0o755); err != nil {
		t.Fatal(err)
	}
}

func mustWrite(t *testing.T, p, content string) {
	t.Helper()
	if err := os.WriteFile(p, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestResolve_QuotedPrefersIncludingDir(t *testing.T) {
	root, r := setupResolverTree(t)

	// app/ 下有 name.h，net/ 下也有 name.h：必须命中包含者同目录。
	res, ok := r.Resolve(filepath.Join(root, "app/common.h"),
		scanner.Include{Path: "name.h", Kind: scanner.Quoted})
	if !ok {
		t.Fatal("expected resolution")
	}
	want := filepath.Join(root, "app/name.h")
	if res.Abs != want {
		t.Errorf("got %s, want %s", res.Abs, want)
	}
	if res.Scope != "relative" {
		t.Errorf("scope = %s, want relative", res.Scope)
	}
}

func TestResolve_QuotedFallsThroughToQuoteDir(t *testing.T) {
	root, r := setupResolverTree(t)

	// app/util/u.h 以引号包含 name.h：util/ 下没有 -> quote dir net/ 命中。
	res, ok := r.Resolve(filepath.Join(root, "app/util/u.h"),
		scanner.Include{Path: "name.h", Kind: scanner.Quoted})
	if !ok {
		t.Fatal("expected resolution via quote dir")
	}
	want := filepath.Join(root, "net/name.h")
	if res.Abs != want {
		t.Errorf("got %s, want %s (scope=%s)", res.Abs, want, res.Scope)
	}
	if res.Scope != "quote" {
		t.Errorf("scope = %s, want quote", res.Scope)
	}
}

func TestResolve_AngledSkipsRelativeAndQuoteDirs(t *testing.T) {
	root, r := setupResolverTree(t)

	// 尖括号：即使同目录有 name.h 也不能命中，只能走 system dirs。
	_, ok := r.Resolve(filepath.Join(root, "app/common.h"),
		scanner.Include{Path: "name.h", Kind: scanner.Angled})
	if ok {
		t.Fatal("angled include must not resolve app/name.h or net/name.h")
	}
	res, ok := r.Resolve(filepath.Join(root, "app/common.h"),
		scanner.Include{Path: "stdio.h", Kind: scanner.Angled})
	if !ok {
		t.Fatal("stdio.h should resolve in system dir")
	}
	if res.Abs != filepath.Join(root, "sys/stdio.h") {
		t.Errorf("got %s", res.Abs)
	}
	if res.Scope != "system" {
		t.Errorf("scope = %s, want system", res.Scope)
	}
}

func TestResolve_NestedPath(t *testing.T) {
	root, r := setupResolverTree(t)
	res, ok := r.Resolve(filepath.Join(root, "app/main.c"),
		scanner.Include{Path: "util/u.h", Kind: scanner.Quoted})
	if !ok {
		t.Fatal("expected nested resolution")
	}
	if res.Abs != filepath.Join(root, "app/util/u.h") {
		t.Errorf("got %s", res.Abs)
	}
}

func TestResolve_Missing(t *testing.T) {
	_, r := setupResolverTree(t)
	if _, ok := r.Resolve("/tmp/whatever.c",
		scanner.Include{Path: "nope.h", Kind: scanner.Quoted}); ok {
		t.Fatal("expected unresolved")
	}
}

func TestFirstCandidate(t *testing.T) {
	root, r := setupResolverTree(t)

	// 引号：首个候选是包含者同目录
	got := r.FirstCandidate(filepath.Join(root, "app/x.c"),
		scanner.Include{Path: "sub/y.h", Kind: scanner.Quoted})
	want := filepath.Join(root, "app/sub/y.h")
	if got != want {
		t.Errorf("quoted candidate = %s, want %s", got, want)
	}

	// 尖括号：首个候选是第一个系统目录
	got = r.FirstCandidate(filepath.Join(root, "app/x.c"),
		scanner.Include{Path: "stdio.h", Kind: scanner.Angled})
	want = filepath.Join(root, "sys/stdio.h")
	if got != want {
		t.Errorf("angled candidate = %s, want %s", got, want)
	}
}

func TestResolve_NonexistentSearchDirsIgnored(t *testing.T) {
	root := t.TempDir()
	mustWrite(t, filepath.Join(root, "a.c"), "")
	r, err := New(root, []string{"does-not-exist"}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(r.QuoteDirs()) != 0 {
		t.Fatalf("nonexistent dir should not be retained, got %v", r.QuoteDirs())
	}
}

func TestIsUnder(t *testing.T) {
	if !IsUnder("/a/b", "/a/b/c.h") {
		t.Error("child should be under")
	}
	if !IsUnder("/a/b", "/a/b") {
		t.Error("self should be under")
	}
	if IsUnder("/a/b", "/a/bb/x") {
		t.Error("sibling with common prefix must not be under")
	}
	if IsUnder("/a/b", "/a/c/x.h") {
		t.Error("../c must not be under")
	}
}

func TestSortedUnique(t *testing.T) {
	got := SortedUnique([]string{"b", "a", "b", "c", "a"})
	want := []string{"a", "b", "c"}
	if len(got) != len(want) {
		t.Fatalf("got %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("got %v, want %v", got, want)
		}
	}
}
