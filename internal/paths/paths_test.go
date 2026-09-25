package paths

import (
	"os"
	"path/filepath"
	"testing"
)

func TestResolveSafe(t *testing.T) {
	root := t.TempDir()
	if err := os.MkdirAll(filepath.Join(root, "sub"), 0o755); err != nil {
		t.Fatal(err)
	}
	cases := []string{"a.txt", "sub/b.txt", "./c.txt", "sub/../d.txt"}
	for _, c := range cases {
		if _, err := Resolve(root, c); err != nil {
			t.Errorf("Resolve(%q): %v", c, err)
		}
	}
}

func TestResolveRejectsTraversal(t *testing.T) {
	root := t.TempDir()
	bad := []string{"../escape", "../../etc/passwd", "a/../../b", ".."}
	for _, c := range bad {
		if _, err := Resolve(root, c); err == nil {
			t.Errorf("Resolve(%q) should fail", c)
		}
	}
}

func TestResolveRejectsAbsolute(t *testing.T) {
	root := t.TempDir()
	if _, err := Resolve(root, "/etc/passwd"); err == nil {
		t.Fatal("absolute path should be rejected")
	}
}

func TestResolveSymlinkEscape(t *testing.T) {
	root := t.TempDir()
	outside := t.TempDir()
	if err := os.WriteFile(filepath.Join(outside, "secret"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	// sub -> outside directory
	if err := os.Symlink(outside, filepath.Join(root, "sub")); err != nil {
		t.Fatal(err)
	}
	if _, err := Resolve(root, "sub/secret"); err == nil {
		t.Fatal("symlink escaping root should be rejected")
	}
}

func TestResolveSymlinkInsideRoot(t *testing.T) {
	root := t.TempDir()
	if err := os.MkdirAll(filepath.Join(root, "real"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Join(root, "real"), filepath.Join(root, "link")); err != nil {
		t.Fatal(err)
	}
	if _, err := Resolve(root, "link/f.txt"); err != nil {
		t.Fatalf("symlink inside root should be allowed: %v", err)
	}
}

func TestResolveFinalSymlinkEscape(t *testing.T) {
	root := t.TempDir()
	outside := filepath.Join(t.TempDir(), "secret")
	if err := os.WriteFile(outside, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(outside, filepath.Join(root, "link")); err != nil {
		t.Fatal(err)
	}
	if _, err := Resolve(root, "link"); err == nil {
		t.Fatal("final-component symlink escaping root should be rejected")
	}
}
