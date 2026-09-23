package digest

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestFileHashIsContentBased(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "f.txt")
	if err := os.WriteFile(p, []byte("hello"), 0o644); err != nil {
		t.Fatal(err)
	}
	h1, err := File(p)
	if err != nil {
		t.Fatal(err)
	}
	want := Bytes([]byte("hello"))
	if h1 != want {
		t.Fatalf("file hash = %s, want %s", h1, want)
	}

	// 仅改变 mtime，内容不变 => 哈希不变。
	old := time.Now().Add(-3 * time.Hour)
	if err := os.Chtimes(p, old, old); err != nil {
		t.Fatal(err)
	}
	h2, err := File(p)
	if err != nil {
		t.Fatal(err)
	}
	if h1 != h2 {
		t.Fatalf("hash changed after mtime-only touch: %s vs %s", h1, h2)
	}

	// 内容改变 => 哈希改变。
	if err := os.WriteFile(p, []byte("hello!"), 0o644); err != nil {
		t.Fatal(err)
	}
	h3, err := File(p)
	if err != nil {
		t.Fatal(err)
	}
	if h3 == h1 {
		t.Fatal("hash did not change after content change")
	}
}

func TestTreeHashIgnoresMtime(t *testing.T) {
	dir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(dir, "sub"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "a.txt"), []byte("A"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "sub", "b.txt"), []byte("B"), 0o644); err != nil {
		t.Fatal(err)
	}
	h1, entries1, err := Tree(dir)
	if err != nil {
		t.Fatal(err)
	}
	old := time.Now().Add(-24 * time.Hour)
	if err := filepath.Walk(dir, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		return os.Chtimes(path, old, old)
	}); err != nil {
		t.Fatal(err)
	}
	h2, _, err := Tree(dir)
	if err != nil {
		t.Fatal(err)
	}
	if h1 != h2 {
		t.Fatalf("tree hash changed on mtime-only touch:\n%s\n%s", h1, h2)
	}
	if len(entries1) != 3 { // a.txt, sub/, sub/b.txt
		t.Fatalf("want 3 entries, got %d: %+v", len(entries1), entries1)
	}
}

func TestCanonicalStable(t *testing.T) {
	a := map[string]any{"x": 1, "y": []int{2, 3}}
	b := map[string]any{"y": []int{2, 3}, "x": 1}
	ha, _ := Canonical(a)
	hb, _ := Canonical(b)
	if ha != hb {
		t.Fatalf("canonical hash unstable across map key order: %s vs %s", ha, hb)
	}
}
