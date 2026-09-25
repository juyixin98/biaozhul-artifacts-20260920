package safeio

import (
	"errors"
	"os"
	"path/filepath"
	"testing"
)

func TestLexicalValidation(t *testing.T) {
	// The lexical helper normalizes cleanable paths; callers (spec.checkRel)
	// reject a path whose cleaned form differs from the declaration.
	cleaned, err := ResolveWithinSafeLexical("a/./b")
	if err != nil || cleaned != "a/b" {
		t.Fatalf("a/./b -> %q, %v", cleaned, err)
	}
	cases := map[string]bool{
		"a/b.txt":   true,
		"../escape": false,
		"/abs/path": false,
		"":          false,
		"a/../../x": false,
	}
	for p, ok := range cases {
		_, err := ResolveWithinSafeLexical(p)
		if ok && err != nil {
			t.Errorf("path %q rejected: %v", p, err)
		}
		if !ok && !errors.Is(err, ErrEscapesBase) {
			t.Errorf("path %q err=%v, want ErrEscapesBase", p, err)
		}
	}
}

func TestResolveWithinSymlinkEscape(t *testing.T) {
	base := t.TempDir()
	if err := os.MkdirAll(filepath.Join(base, "sub"), 0o755); err != nil {
		t.Fatal(err)
	}
	target := filepath.Join(t.TempDir(), "secret")
	if err := os.WriteFile(target, []byte("secret"), 0o644); err != nil {
		t.Fatal(err)
	}
	// Symlink inside base pointing outside.
	if err := os.Symlink(target, filepath.Join(base, "sub", "link")); err != nil {
		t.Fatal(err)
	}
	if _, err := ResolveWithin(base, "sub/link"); !errors.Is(err, ErrEscapesBase) {
		t.Fatalf("symlink escape err=%v, want ErrEscapesBase", err)
	}
	// Plain contained path works.
	if err := os.WriteFile(filepath.Join(base, "sub", "f.txt"), []byte("ok"), 0o644); err != nil {
		t.Fatal(err)
	}
	got, err := ResolveWithin(base, "sub/f.txt")
	if err != nil {
		t.Fatalf("contained path rejected: %v", err)
	}
	if b, err := os.ReadFile(got); err != nil || string(b) != "ok" {
		t.Fatalf("read = %q, %v", b, err)
	}
}
