package batch

import (
	"crypto/sha256"
	"encoding/hex"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
)

func req(diff string) Request { return Request{Diff: diff} }

// fingerprint walks the work directory and hashes every regular file's
// relative path, mode and content, yielding an order-independent digest.
func fingerprint(t *testing.T, root string) string {
	t.Helper()
	type entry struct{ rel, mode, sum string }
	var entries []entry
	err := filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if path == root {
			return nil
		}
		rel, _ := filepath.Rel(root, path)
		info, err := d.Info()
		if err != nil {
			return err
		}
		mode := info.Mode().Perm()
		sum := ""
		if d.Type().IsRegular() {
			data, err := os.ReadFile(path)
			if err != nil {
				return err
			}
			h := sha256.Sum256(data)
			sum = hex.EncodeToString(h[:])
		}
		entries = append(entries, entry{filepath.ToSlash(rel), mode.String(), sum})
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	sort.Slice(entries, func(i, j int) bool { return entries[i].rel < entries[j].rel })
	var b strings.Builder
	for _, e := range entries {
		b.WriteString(e.rel)
		b.WriteByte('|')
		b.WriteString(e.mode)
		b.WriteByte('|')
		b.WriteString(e.sum)
		b.WriteByte('\n')
	}
	h := sha256.Sum256([]byte(b.String()))
	return hex.EncodeToString(h[:])
}

func writeFile(t *testing.T, root, rel, content string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Join(root, filepath.Dir(rel)), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, rel), []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestBatchApplySuccess(t *testing.T) {
	root := t.TempDir()
	writeFile(t, root, "a.txt", "one\ntwo\nthree\n")
	writeFile(t, root, "dir/b.txt", "x\n")

	patches := []Request{
		req("--- a/a.txt\n+++ b/a.txt\n@@ -1,3 +1,3 @@\n one\n-two\n+TWO\n three\n"),
		req("--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1,1 @@\n+fresh\n"),
		req("--- a/dir/b.txt\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-x\n"),
	}
	plan, err := PlanBatch(root, t.TempDir(), patches)
	if err != nil {
		t.Fatalf("plan: %v", err)
	}
	if err := plan.Commit(); err != nil {
		t.Fatalf("commit: %v", err)
	}
	if got := readFile(t, filepath.Join(root, "a.txt")); got != "one\nTWO\nthree\n" {
		t.Fatalf("a.txt = %q", got)
	}
	if got := readFile(t, filepath.Join(root, "new.txt")); got != "fresh\n" {
		t.Fatalf("new.txt = %q", got)
	}
	if _, err := os.Stat(filepath.Join(root, "dir/b.txt")); !os.IsNotExist(err) {
		t.Fatalf("b.txt should be deleted, err=%v", err)
	}
}

func TestBatchValidationFailureLeavesDirUntouched(t *testing.T) {
	root := t.TempDir()
	writeFile(t, root, "good.txt", "a\nb\n")
	writeFile(t, root, "bad.txt", "a\nb\n")
	before := fingerprint(t, root)

	patches := []Request{
		req("--- a/good.txt\n+++ b/good.txt\n@@ -1,2 +1,2 @@\n a\n-b\n+B\n"),
		// wrong context on the second file
		req("--- a/bad.txt\n+++ b/bad.txt\n@@ -1,2 +1,2 @@\n WRONG\n-b\n+B\n"),
	}
	if _, err := PlanBatch(root, t.TempDir(), patches); err == nil {
		t.Fatal("expected plan failure")
	}
	after := fingerprint(t, root)
	if before != after {
		t.Fatalf("work directory changed after failed validation\nbefore %s\nafter  %s", before, after)
	}
}

func TestBatchPublishFailureRollback(t *testing.T) {
	root := t.TempDir()
	writeFile(t, root, "a.txt", "a\nb\n")
	writeFile(t, root, "victim.txt", "keep\n")
	before := fingerprint(t, root)

	patches := []Request{
		req("--- a/a.txt\n+++ b/a.txt\n@@ -1,2 +1,2 @@\n a\n-b\n+B\n"),
		req("--- a/victim.txt\n+++ b/victim.txt\n@@ -1,1 +1,1 @@\n-keep\n+KEPT\n"),
	}
	cache := t.TempDir()
	plan, err := PlanBatch(root, cache, patches)
	if err != nil {
		t.Fatal(err)
	}
	// Sabotage: make the staged blob of victim.txt unreadable. Publish
	// order is path-sorted, so a.txt is installed first; the victim install
	// fails during the copy step and rollback must restore a.txt.
	blobs, err := os.ReadDir(cache)
	if err != nil || len(blobs) != 2 {
		t.Fatalf("expected 2 staged blobs, got %v (err %v)", blobs, err)
	}
	var victimBlob string
	// Blobs are ordered only by counter; identify them by content.
	for _, b := range blobs {
		p := filepath.Join(cache, b.Name())
		data, _ := os.ReadFile(p)
		if string(data) == "keep\n" || string(data) == "KEPT\n" {
			if string(data) == "KEPT\n" {
				victimBlob = p
			}
		}
	}
	if victimBlob == "" {
		t.Fatal("could not locate victim blob")
	}
	if err := os.Chmod(victimBlob, 0); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chmod(victimBlob, 0o600) })
	if err := plan.Commit(); err == nil {
		t.Fatal("expected commit failure")
	}
	after := fingerprint(t, root)
	if before != after {
		t.Fatalf("work directory not restored after rollback\nbefore %s\nafter  %s", before, after)
	}
	if got := readFile(t, filepath.Join(root, "a.txt")); got != "a\nb\n" {
		t.Fatalf("a.txt should be rolled back, got %q", got)
	}
	// No backup/temp leftovers anywhere in the tree.
	filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err == nil && strings.HasPrefix(d.Name(), ".patchd-") {
			t.Errorf("leftover artifact: %s", path)
		}
		return nil
	})
}

func TestBatchDuplicateTargetRejected(t *testing.T) {
	root := t.TempDir()
	writeFile(t, root, "f.txt", "a\n")
	p := []Request{
		req("--- a/f.txt\n+++ b/f.txt\n@@ -1,1 +1,1 @@\n-a\n+A\n"),
		req("--- a/f.txt\n+++ b/f.txt\n@@ -1,1 +1,1 @@\n-a\n+C\n"),
	}
	if _, err := PlanBatch(root, t.TempDir(), p); err == nil {
		t.Fatal("expected duplicate target error")
	}
}

func TestBatchPathTraversalRejected(t *testing.T) {
	root := t.TempDir()
	p := []Request{
		req("--- a/../evil\n+++ b/../evil\n@@ -1,0 +1,1 @@\n+x\n"),
	}
	if _, err := PlanBatch(root, t.TempDir(), p); err == nil {
		t.Fatal("expected unsafe path error")
	}
}

func TestBatchMalformedDiffRejected(t *testing.T) {
	root := t.TempDir()
	if _, err := PlanBatch(root, t.TempDir(), []Request{{Diff: "this is not a diff\n"}}); err == nil {
		t.Fatal("expected parse error")
	}
}

func TestBatchNoNewlineRoundTrip(t *testing.T) {
	root := t.TempDir()
	writeFile(t, root, "f", "a\nb")
	p := []Request{req("--- a/f\n+++ b/f\n@@ -1,2 +1,2 @@\n a\n-b\n\\ No newline at end of file\n+B\n\\ No newline at end of file\n")}
	plan, err := PlanBatch(root, t.TempDir(), p)
	if err != nil {
		t.Fatal(err)
	}
	if err := plan.Commit(); err != nil {
		t.Fatal(err)
	}
	got, err := os.ReadFile(filepath.Join(root, "f"))
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != "a\nB" {
		t.Fatalf("got %q", got)
	}
}

func TestBatchCacheSeparateFromWorkdir(t *testing.T) {
	root := t.TempDir()
	cache := t.TempDir()
	writeFile(t, root, "f.txt", "a\n")
	p := []Request{req("--- a/f.txt\n+++ b/f.txt\n@@ -1,1 +1,1 @@\n-a\n+A\n")}
	plan, err := PlanBatch(root, cache, p)
	if err != nil {
		t.Fatal(err)
	}
	// After staging, the cache holds a blob; workdir is untouched.
	staged, _ := os.ReadDir(cache)
	if len(staged) == 0 {
		t.Fatal("expected staged blobs in cache")
	}
	if got := readFile(t, filepath.Join(root, "f.txt")); got != "a\n" {
		t.Fatalf("workdir changed before commit: %q", got)
	}
	if err := plan.Commit(); err != nil {
		t.Fatal(err)
	}
	// Blobs are cleaned up after a successful commit.
	staged, _ = os.ReadDir(cache)
	if len(staged) != 0 {
		t.Fatalf("cache not cleaned after commit: %d entries", len(staged))
	}
}

func TestBatchRollbackAfterModifyAndDelete(t *testing.T) {
	root := t.TempDir()
	writeFile(t, root, "a.txt", "a\nb\n")
	writeFile(t, root, "gone.txt", "g\n")
	writeFile(t, root, "victim.txt", "keep\n")
	before := fingerprint(t, root)

	patches := []Request{
		req("--- a/a.txt\n+++ b/a.txt\n@@ -1,2 +1,2 @@\n a\n-b\n+B\n"),
		req("--- a/gone.txt\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-g\n"),
		req("--- a/victim.txt\n+++ b/victim.txt\n@@ -1,1 +1,1 @@\n-keep\n+KEPT\n"),
	}
	cache := t.TempDir()
	plan, err := PlanBatch(root, cache, patches)
	if err != nil {
		t.Fatal(err)
	}
	// Sabotage the victim's staged blob so it fails third (after modify
	// and delete have been published).
	for _, b := range mustReadDir(t, cache) {
		p := filepath.Join(cache, b.Name())
		data, _ := os.ReadFile(p)
		if string(data) == "KEPT\n" {
			if err := os.Chmod(p, 0); err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() { _ = os.Chmod(p, 0o600) })
		}
	}
	if err := plan.Commit(); err == nil {
		t.Fatal("expected commit failure")
	}
	if fingerprint(t, root) != before {
		t.Fatal("work directory not fully restored after multi-step rollback")
	}
	if got := readFile(t, filepath.Join(root, "gone.txt")); got != "g\n" {
		t.Fatalf("deleted file not restored: %q", got)
	}
}

func mustReadDir(t *testing.T, dir string) []os.DirEntry {
	t.Helper()
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	return entries
}

func readFile(t *testing.T, path string) string {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}
