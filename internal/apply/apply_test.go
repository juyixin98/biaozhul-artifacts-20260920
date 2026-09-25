package apply

import (
	"crypto/sha256"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
)

// mustPlanApply runs Plan then Publish and returns the changes.
func mustPlanApply(t *testing.T, root, cache string, patches ...string) []Change {
	t.Helper()
	changes, err := Plan(root, patches)
	if err != nil {
		t.Fatalf("Plan: %v", err)
	}
	if err := Publish(root, changes, cache); err != nil {
		t.Fatalf("Publish: %v", err)
	}
	return changes
}

// dirSnapshot is a byte-level snapshot of every regular file in the work
// directory, including permissions and relative path ordering.
type fileEntry struct {
	sum  string
	mode os.FileMode
	size int64
}

type dirSnapshot map[string]fileEntry

func snapshot(t *testing.T, root string) dirSnapshot {
	t.Helper()
	snap := dirSnapshot{}
	err := filepath.Walk(root, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		rel, rerr := filepath.Rel(root, path)
		if rerr != nil {
			return rerr
		}
		if rel == "." {
			return nil
		}
		if info.IsDir() {
			snap[rel+"/"] = fileEntry{mode: info.Mode().Perm()}
			return nil
		}
		data, rerr := os.ReadFile(path)
		if rerr != nil {
			return rerr
		}
		sum := sha256.Sum256(data)
		snap[rel] = fileEntry{sum: fmt.Sprintf("%x", sum), mode: info.Mode().Perm(), size: info.Size()}
		return nil
	})
	if err != nil {
		t.Fatalf("snapshot: %v", err)
	}
	return snap
}

func (s dirSnapshot) assertUnchanged(t *testing.T, root, where string) {
	t.Helper()
	now := snapshot(t, root)
	if len(now) != len(s) {
		t.Fatalf("directory changed after %s: had %d entries, now %d\nbefore: %v\nafter:  %v",
			where, len(s), len(now), keys(s), keys(now))
	}
	for path, before := range s {
		after, ok := now[path]
		if !ok {
			t.Fatalf("after %s: %s disappeared", where, path)
		}
		if after != before {
			t.Fatalf("after %s: %s changed: before=%+v after=%+v", where, path, before, after)
		}
	}
}

func keys(m dirSnapshot) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func writeFile(t *testing.T, path, content string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

func readFile(t *testing.T, path string) string {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

// TestModifyMiddleAndEnd exercises ordinary context matching and exact
// line numbers over multiple hunks.
func TestModifyMiddleAndEnd(t *testing.T) {
	root := t.TempDir()
	cache := t.TempDir()
	writeFile(t, filepath.Join(root, "f.txt"), "one\ntwo\nthree\nfour\n")

	patch := `--- a/f.txt
+++ b/f.txt
@@ -1,3 +1,3 @@
 one
-two
+TWO
 three
@@ -4 +4 @@
-four
+FOUR
`
	mustPlanApply(t, root, cache, patch)
	if got := readFile(t, filepath.Join(root, "f.txt")); got != "one\nTWO\nthree\nFOUR\n" {
		t.Fatalf("result = %q", got)
	}
}

// TestNoTrailingNewlineRemove covers the acceptance case: old file without a
// final newline, hunk carrying "\ No newline at end of file" on both sides,
// result also without a trailing newline.
func TestNoTrailingNewlineRemove(t *testing.T) {
	root := t.TempDir()
	cache := t.TempDir()
	writeFile(t, filepath.Join(root, "a.txt"), "first\nlast")

	patch := `--- a/a.txt
+++ b/a.txt
@@ -1,2 +1,2 @@
 first
-last
\ No newline at end of file
+LAST
\ No newline at end of file
`
	mustPlanApply(t, root, cache, patch)
	got := readFile(t, filepath.Join(root, "a.txt"))
	if got != "first\nLAST" {
		t.Fatalf("result = %q, want %q", got, "first\nLAST")
	}
	if strings.HasSuffix(got, "\n") {
		t.Fatal("result unexpectedly ends with newline")
	}
}

// TestAddTrailingNewline: file with no newline becomes a file with one.
func TestAddTrailingNewline(t *testing.T) {
	root := t.TempDir()
	cache := t.TempDir()
	writeFile(t, filepath.Join(root, "a.txt"), "line")

	patch := `--- a/a.txt
+++ b/a.txt
@@ -1 +1 @@
-line
\ No newline at end of file
+line
`
	mustPlanApply(t, root, cache, patch)
	if got := readFile(t, filepath.Join(root, "a.txt")); got != "line\n" {
		t.Fatalf("result = %q, want trailing newline", got)
	}
}

// TestRemoveTrailingNewline: file ending in newline becomes one without.
func TestRemoveTrailingNewline(t *testing.T) {
	root := t.TempDir()
	cache := t.TempDir()
	writeFile(t, filepath.Join(root, "a.txt"), "line\n")

	patch := `--- a/a.txt
+++ b/a.txt
@@ -1 +1 @@
-line
+line
\ No newline at end of file
`
	mustPlanApply(t, root, cache, patch)
	if got := readFile(t, filepath.Join(root, "a.txt")); got != "line" {
		t.Fatalf("result = %q, want no trailing newline", got)
	}
}

// TestNoNewlineMarkerMustMatchFile: a marker claiming no trailing newline
// while the file has one is rejected (no fuzzy guessing).
func TestNoNewlineMarkerMustMatchFile(t *testing.T) {
	root := t.TempDir()
	writeFile(t, filepath.Join(root, "a.txt"), "first\nlast\n")
	before := snapshot(t, root)

	patch := `--- a/a.txt
+++ b/a.txt
@@ -1,2 +1,2 @@
 first
-last
\ No newline at end of file
+LAST
\ No newline at end of file
`
	if _, err := Plan(root, []string{patch}); err == nil {
		t.Fatal("Plan succeeded with mismatched no-newline marker, want error")
	}
	before.assertUnchanged(t, root, "marker mismatch")
}

// TestAppendToNoNewlineFile covers the exact shape git diff produces when
// appending to a file that lacks a trailing newline: the context line gets a
// marker (old side), and the added line carries the new side's EOL state.
func TestAppendToNoNewlineFile(t *testing.T) {
	root := t.TempDir()
	cache := t.TempDir()
	writeFile(t, filepath.Join(root, "a.txt"), "only")

	patch := `--- a/a.txt
+++ b/a.txt
@@ -1 +1,2 @@
 only
\ No newline at end of file
+second
`
	mustPlanApply(t, root, cache, patch)
	if got := readFile(t, filepath.Join(root, "a.txt")); got != "only\nsecond\n" {
		t.Fatalf("result = %q, want %q", got, "only\nsecond\n")
	}
}

// TestAppendToNoNewlineFileKeepNoNewline: same shape, but the added last
// line also carries a marker, so the result stays without trailing newline.
func TestAppendToNoNewlineFileKeepNoNewline(t *testing.T) {
	root := t.TempDir()
	cache := t.TempDir()
	writeFile(t, filepath.Join(root, "a.txt"), "only")

	patch := `--- a/a.txt
+++ b/a.txt
@@ -1 +1,2 @@
 only
\ No newline at end of file
+second
\ No newline at end of file
`
	mustPlanApply(t, root, cache, patch)
	if got := readFile(t, filepath.Join(root, "a.txt")); got != "only\nsecond" {
		t.Fatalf("result = %q, want %q", got, "only\nsecond")
	}
}

func TestNewFileWithNewline(t *testing.T) {
	root := t.TempDir()
	cache := t.TempDir()

	patch := `--- /dev/null
+++ b/sub/new.txt
@@ -0,0 +1,2 @@
+alpha
+beta
`
	changes := mustPlanApply(t, root, cache, patch)
	if len(changes) != 1 || changes[0].Kind != Create || changes[0].Path != "sub/new.txt" {
		t.Fatalf("changes = %+v", changes)
	}
	if got := readFile(t, filepath.Join(root, "sub/new.txt")); got != "alpha\nbeta\n" {
		t.Fatalf("result = %q", got)
	}
}

func TestNewFileNoTrailingNewline(t *testing.T) {
	root := t.TempDir()
	cache := t.TempDir()

	patch := `--- /dev/null
+++ b/one.txt
@@ -0,0 +1 @@
+single
\ No newline at end of file
`
	mustPlanApply(t, root, cache, patch)
	if got := readFile(t, filepath.Join(root, "one.txt")); got != "single" {
		t.Fatalf("result = %q", got)
	}
}

func TestCreateRejectsExistingFile(t *testing.T) {
	root := t.TempDir()
	writeFile(t, filepath.Join(root, "x.txt"), "exists\n")
	before := snapshot(t, root)

	patch := `--- /dev/null
+++ b/x.txt
@@ -0,0 +1 @@
+exists
`
	if _, err := Plan(root, []string{patch}); err == nil {
		t.Fatal("Plan created over existing file, want error")
	}
	before.assertUnchanged(t, root, "create-existing")
}

func TestDeleteFile(t *testing.T) {
	root := t.TempDir()
	cache := t.TempDir()
	writeFile(t, filepath.Join(root, "goner.txt"), "a\nb\n")

	patch := `--- a/goner.txt
+++ /dev/null
@@ -1,2 +0,0 @@
-a
-b
`
	changes := mustPlanApply(t, root, cache, patch)
	if changes[0].Kind != Delete {
		t.Fatalf("kind = %s, want delete", changes[0].Kind)
	}
	if _, err := os.Stat(filepath.Join(root, "goner.txt")); !os.IsNotExist(err) {
		t.Fatalf("file still exists: %v", err)
	}
}

func TestDeleteNoTrailingNewline(t *testing.T) {
	root := t.TempDir()
	cache := t.TempDir()
	writeFile(t, filepath.Join(root, "goner.txt"), "a\nb")

	patch := `--- a/goner.txt
+++ /dev/null
@@ -1,2 +0,0 @@
-a
-b
\ No newline at end of file
`
	mustPlanApply(t, root, cache, patch)
	if _, err := os.Stat(filepath.Join(root, "goner.txt")); !os.IsNotExist(err) {
		t.Fatal("file still exists")
	}
}

// TestDeletePatchMustRemoveAllLines rejects a deletion hunk that does not
// cover the whole file.
func TestDeletePatchMustRemoveAllLines(t *testing.T) {
	root := t.TempDir()
	writeFile(t, filepath.Join(root, "goner.txt"), "a\nb\n")
	before := snapshot(t, root)

	patch := `--- a/goner.txt
+++ /dev/null
@@ -1 +0,0 @@
-a
`
	if _, err := Plan(root, []string{patch}); err == nil {
		t.Fatal("Plan accepted partial deletion, want error")
	}
	before.assertUnchanged(t, root, "partial delete")
}

func TestModifyMissingFile(t *testing.T) {
	root := t.TempDir()
	patch := `--- a/missing.txt
+++ b/missing.txt
@@ -1 +1 @@
-a
+b
`
	if _, err := Plan(root, []string{patch}); err == nil {
		t.Fatal("Plan modified a missing file, want error")
	}
}

// TestOverlappingHunks: two hunks whose old-line ranges intersect must be
// rejected and nothing may change.
func TestOverlappingHunks(t *testing.T) {
	root := t.TempDir()
	writeFile(t, filepath.Join(root, "f.txt"), "l1\nl2\nl3\nl4\n")
	before := snapshot(t, root)

	patch := `--- a/f.txt
+++ b/f.txt
@@ -1,3 +1,3 @@
 l1
-l2
+L2
 l3
@@ -3,2 +3,2 @@
 l3
-l4
+L4
`
	_, err := Plan(root, []string{patch})
	if err == nil || !strings.Contains(err.Error(), "overlap") {
		t.Fatalf("err = %v, want overlap error", err)
	}
	before.assertUnchanged(t, root, "overlap")
}

// TestAdjacentHunksAreAllowed: hunks that touch but do not interleave are
// fine (line 3 ends one, line 4 starts the next).
func TestAdjacentHunksAreAllowed(t *testing.T) {
	root := t.TempDir()
	cache := t.TempDir()
	writeFile(t, filepath.Join(root, "f.txt"), "l1\nl2\nl3\nl4\n")

	patch := `--- a/f.txt
+++ b/f.txt
@@ -1,3 +1,3 @@
 l1
-l2
+L2
 l3
@@ -4 +4 @@
-l4
+L4
`
	mustPlanApply(t, root, cache, patch)
	if got := readFile(t, filepath.Join(root, "f.txt")); got != "l1\nL2\nl3\nL4\n" {
		t.Fatalf("result = %q", got)
	}
}

// TestWrongContext is the acceptance case "错误上下文": the context line at
// the declared line number does not match; the service must not search for
// the line elsewhere.
func TestWrongContext(t *testing.T) {
	root := t.TempDir()
	writeFile(t, filepath.Join(root, "f.txt"), "one\ntwo\nthree\n")
	before := snapshot(t, root)

	patch := `--- a/f.txt
+++ b/f.txt
@@ -1,3 +1,3 @@
 one
-wrong line
+TWO
 three
`
	_, err := Plan(root, []string{patch})
	var mm *ContextMismatchError
	if err == nil {
		t.Fatal("Plan succeeded with wrong context, want error")
	}
	if !errors.As(err, &mm) {
		t.Fatalf("error %v is not a ContextMismatchError", err)
	}
	if mm.Line != 2 || mm.Expected != "two" || mm.Found != "wrong line" {
		t.Fatalf("mismatch = %+v", mm)
	}
	before.assertUnchanged(t, root, "wrong context")
}

// TestWrongLineNumber: the hunk header points beyond EOF.
func TestWrongLineNumber(t *testing.T) {
	root := t.TempDir()
	writeFile(t, filepath.Join(root, "f.txt"), "only line\n")
	before := snapshot(t, root)

	patch := `--- a/f.txt
+++ b/f.txt
@@ -5,3 +5,3 @@
 a
-b
+c
`
	_, err := Plan(root, []string{patch})
	if err == nil {
		t.Fatal("Plan accepted out-of-range hunk, want error")
	}
	before.assertUnchanged(t, root, "bad line number")
}

// TestNoFuzzyRelocation: the expected context exists at a different line but
// the header names line 1; exact semantics must reject it.
func TestNoFuzzyRelocation(t *testing.T) {
	root := t.TempDir()
	writeFile(t, filepath.Join(root, "f.txt"), "junk\ntarget line\nmore\n")
	before := snapshot(t, root)

	patch := `--- a/f.txt
+++ b/f.txt
@@ -1,2 +1,2 @@
-target line
+TARGET
 more
`
	_, err := Plan(root, []string{patch})
	if err == nil {
		t.Fatal("Plan fuzzy-matched context at the wrong line number, want error")
	}
	before.assertUnchanged(t, root, "fuzzy relocation")
}

func TestInvalidPaths(t *testing.T) {
	cases := []string{
		"",
		"/etc/passwd",
		"../evil",
		"a/../../b",
		"a//b",
		"./a",
		"a/./b",
		"a\\b",
	}
	for _, p := range cases {
		if err := ValidateRelPath(p); err == nil {
			t.Errorf("ValidateRelPath(%q) = nil, want error", p)
		}
	}
	good := []string{"a.txt", "dir/a.txt", "a/b/c", "x..y", ".hidden", "a/b..c/d"}
	for _, p := range good {
		if err := ValidateRelPath(p); err != nil {
			t.Errorf("ValidateRelPath(%q) = %v, want nil", p, err)
		}
	}
}

// TestBatchAtomicity is the central acceptance check: a batch touching
// several files must leave the whole directory byte-for-byte identical when
// any single file patch fails validation - including when the earlier file
// in the batch was valid and publishable on its own.
func TestBatchAtomicity(t *testing.T) {
	root := t.TempDir()
	cache := t.TempDir()
	writeFile(t, filepath.Join(root, "a.txt"), "alpha\n")
	writeFile(t, filepath.Join(root, "b.txt"), "beta\nb2\n")
	writeFile(t, filepath.Join(root, "dir/c.txt"), "gamma\n")
	before := snapshot(t, root)

	good := `--- a/a.txt
+++ b/a.txt
@@ -1 +1 @@
-alpha
+ALPHA
`
	bad := `--- a/b.txt
+++ b/b.txt
@@ -1,2 +1,2 @@
 WRONG CONTEXT
-beta
+BETA
 b2
`
	if _, err := Plan(root, []string{good, bad}); err == nil {
		t.Fatal("batch validation succeeded with a bad patch, want error")
	}
	// Publish is never reached after Plan failure, but assert disk
	// equality regardless.
	before.assertUnchanged(t, root, "batch validation failure")

	// Run the whole Apply helper for good measure.
	if _, err := Apply(root, cache, []string{good, bad}); err == nil {
		t.Fatal("Apply succeeded with a bad batch, want error")
	}
	before.assertUnchanged(t, root, "Apply failure")
}

// TestDuplicatePathInBatchRejected: same file twice in one batch is refused
// rather than sequentially mutated.
func TestDuplicatePathInBatchRejected(t *testing.T) {
	root := t.TempDir()
	cache := t.TempDir()
	writeFile(t, filepath.Join(root, "a.txt"), "x\ny\n")
	before := snapshot(t, root)

	p1 := "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-x\n+X\n"
	p2 := "--- a/a.txt\n+++ b/a.txt\n@@ -2 +2 @@\n-y\n+Y\n"
	if _, err := Apply(root, cache, []string{p1, p2}); err == nil {
		t.Fatal("duplicate path in batch accepted, want error")
	}
	before.assertUnchanged(t, root, "duplicate path")
}

// TestMultiFileBatchCommit: a valid multi-file batch publishes every change
// and creates/deletes files in one commit.
func TestMultiFileBatchCommit(t *testing.T) {
	root := t.TempDir()
	cache := t.TempDir()
	writeFile(t, filepath.Join(root, "keep.txt"), "a\nb\n")
	writeFile(t, filepath.Join(root, "del.txt"), "gone\n")

	batch := []string{
		"--- a/keep.txt\n+++ b/keep.txt\n@@ -1 +1 @@\n-a\n+A\n",
		"--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+fresh\n",
		"--- a/del.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-gone\n",
	}
	changes := mustPlanApply(t, root, cache, batch...)
	if len(changes) != 3 {
		t.Fatalf("got %d changes, want 3", len(changes))
	}
	if got := readFile(t, filepath.Join(root, "keep.txt")); got != "A\nb\n" {
		t.Fatalf("keep.txt = %q", got)
	}
	if got := readFile(t, filepath.Join(root, "new.txt")); got != "fresh\n" {
		t.Fatalf("new.txt = %q", got)
	}
	if _, err := os.Stat(filepath.Join(root, "del.txt")); !os.IsNotExist(err) {
		t.Fatal("del.txt still exists")
	}
}

// TestPublishCacheMustBeSeparate enforces the cache/work-directory separation
// invariant.
func TestPublishCacheMustBeSeparate(t *testing.T) {
	root := t.TempDir()
	writeFile(t, filepath.Join(root, "f.txt"), "a\n")
	patch := "--- a/f.txt\n+++ b/f.txt\n@@ -1 +1 @@\n-a\n+b\n"
	changes, err := Plan(root, []string{patch})
	if err != nil {
		t.Fatalf("Plan: %v", err)
	}
	// Cache dir inside the work dir must be rejected.
	inside := filepath.Join(root, "cache")
	if err := os.MkdirAll(inside, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := Publish(root, changes, inside); err == nil {
		t.Fatal("Publish accepted cache dir inside work dir, want error")
	}
	// Work dir inside the cache dir must be rejected too.
	cache := t.TempDir()
	nested := filepath.Join(cache, "work")
	if err := os.MkdirAll(nested, 0o755); err != nil {
		t.Fatal(err)
	}
	writeFile(t, filepath.Join(nested, "f.txt"), "a\n")
	changesNested, err := Plan(nested, []string{patch})
	if err != nil {
		t.Fatalf("Plan: %v", err)
	}
	if err := Publish(nested, changesNested, cache); err == nil {
		t.Fatal("Publish accepted work dir inside cache dir, want error")
	}
}

// TestPublishRollback simulates a publish failure (read-only target dir at
// create time) and checks rollback restores every already-created file.
func TestPublishRollback(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("running as root; permission-based rollback test is not meaningful")
	}
	root := t.TempDir()
	cache := t.TempDir()

	// Synthesize changes directly: first a create that will succeed, then a
	// create inside a read-only directory that must fail.
	blocked := filepath.Join(root, "blocked")
	if err := os.MkdirAll(blocked, 0o755); err != nil {
		t.Fatal(err)
	}
	changes := []Change{
		{Path: "early.txt", Kind: Create, Content: []byte("early\n")},
		{Path: filepath.Join("blocked", "late.txt"), Kind: Create, Content: []byte("late\n")},
	}
	if err := os.Chmod(blocked, 0o555); err != nil {
		t.Fatal(err)
	}
	defer os.Chmod(blocked, 0o755)

	err := Publish(root, changes, cache)
	if err == nil {
		t.Fatal("Publish succeeded in a read-only directory, want error")
	}
	if _, err := os.Stat(filepath.Join(root, "early.txt")); !os.IsNotExist(err) {
		t.Fatalf("rollback failed: early.txt still present after publish error: %v", err)
	}
}

func TestEmptyBatchRejected(t *testing.T) {
	if _, err := Plan(t.TempDir(), nil); err == nil {
		t.Fatal("Plan(nil) succeeded, want error")
	}
}
