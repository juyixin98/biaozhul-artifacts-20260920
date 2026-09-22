package storage

import (
	"errors"
	"os"
	"path/filepath"
	"testing"
)

func TestSafeJoin_RejectsTraversal(t *testing.T) {
	root := t.TempDir()
	bad := []string{"../x", "..", ".", "a/b", `a\b`, "", ".hidden", "/abs"}
	for _, name := range bad {
		if _, err := SafeJoin(root, name); err == nil {
			t.Fatalf("SafeJoin(%q) succeeded, want error", name)
		}
	}
}

func TestSafeJoin_AcceptsPlainName(t *testing.T) {
	root := t.TempDir()
	got, err := SafeJoin(root, "frame.png")
	if err != nil {
		t.Fatal(err)
	}
	if filepath.Dir(got) != root {
		t.Fatalf("joined outside root: %s", got)
	}
}

func TestSaveReader_AtomicAndContent(t *testing.T) {
	dir := t.TempDir()
	dst := filepath.Join(dir, "sub", "f.png")
	n, err := SaveReader(dst, bytesReader([]byte("hello")))
	if err != nil {
		t.Fatal(err)
	}
	if n != 5 {
		t.Fatalf("n=%d", n)
	}
	got, err := os.ReadFile(dst)
	if err != nil || string(got) != "hello" {
		t.Fatalf("content=%q err=%v", got, err)
	}
	// No leftover temp files.
	entries, _ := os.ReadDir(filepath.Join(dir, "sub"))
	for _, e := range entries {
		if e.Name() != "f.png" {
			t.Fatalf("leftover file %s", e.Name())
		}
	}
}

func TestSHA256File(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "x")
	if err := os.WriteFile(p, []byte("abc"), 0o644); err != nil {
		t.Fatal(err)
	}
	sum, n, err := SHA256File(p)
	if err != nil {
		t.Fatal(err)
	}
	// sha256("abc")
	if sum != "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad" {
		t.Fatalf("unexpected sha %s", sum)
	}
	if n != 3 {
		t.Fatalf("n=%d", n)
	}
}

func TestRemoveIfExists(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "x")
	if err := os.WriteFile(p, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := RemoveIfExists(p); err != nil {
		t.Fatal(err)
	}
	if err := RemoveIfExists(p); err != nil {
		t.Fatalf("removing again should be nil, got %v", err)
	}
	if _, err := os.Stat(filepath.Join(dir, "missing")); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("sanity")
	}
}
