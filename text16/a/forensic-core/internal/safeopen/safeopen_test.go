package safeopen_test

import (
	"errors"
	"os"
	"path/filepath"
	"testing"

	"forensiccore/internal/safeopen"
)

func setupRoot(t *testing.T) (*safeopen.Root, string, string) {
	t.Helper()
	rootDir := t.TempDir()
	outside := t.TempDir()

	mustWrite := func(p string) {
		t.Helper()
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte("evidence-bytes"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	mustWrite(filepath.Join(rootDir, "ok.dd"))
	mustWrite(filepath.Join(rootDir, "ok.raw"))
	mustWrite(filepath.Join(rootDir, "sub", "nested.dd"))
	mustWrite(filepath.Join(rootDir, "notes.txt"))
	mustWrite(filepath.Join(outside, "secret.dd"))

	// 指向根目录外的符号链接（文件与目录各一）。
	if err := os.Symlink(filepath.Join(outside, "secret.dd"), filepath.Join(rootDir, "escape.dd")); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(outside, filepath.Join(rootDir, "escape-dir")); err != nil {
		t.Fatal(err)
	}
	// 根目录内的合法符号链接。
	if err := os.Symlink("ok.dd", filepath.Join(rootDir, "alias.dd")); err != nil {
		t.Fatal(err)
	}

	root, err := safeopen.NewRoot(rootDir)
	if err != nil {
		t.Fatal(err)
	}
	return root, rootDir, outside
}

func TestOpenValidImage(t *testing.T) {
	root, _, _ := setupRoot(t)
	for _, rel := range []string{"ok.dd", "ok.raw", "sub/nested.dd", "./ok.dd", "alias.dd"} {
		f, id, err := root.OpenReadOnly(rel)
		if err != nil {
			t.Fatalf("%s: %v", rel, err)
		}
		if id.Size != int64(len("evidence-bytes")) {
			t.Fatalf("%s: size = %d", rel, id.Size)
		}
		f.Close()
	}
}

func TestRejectPathTraversal(t *testing.T) {
	root, _, outside := setupRoot(t)
	bad := []string{
		"../" + filepath.Base(outside) + "/secret.dd",
		"../../etc/passwd",
		"sub/../../escape.dd",
		"/etc/passwd",
		filepath.Join(outside, "secret.dd"), // 绝对路径
		"..",
		"",
	}
	for _, rel := range bad {
		if _, _, err := root.OpenReadOnly(rel); !errors.Is(err, safeopen.ErrOutsideRoot) {
			t.Fatalf("%q: err = %v, want ErrOutsideRoot", rel, err)
		}
	}
}

func TestRejectSymlinkEscape(t *testing.T) {
	root, _, _ := setupRoot(t)
	for _, rel := range []string{"escape.dd", "escape-dir/secret.dd"} {
		if _, _, err := root.OpenReadOnly(rel); !errors.Is(err, safeopen.ErrOutsideRoot) {
			t.Fatalf("%q: err = %v, want ErrOutsideRoot", rel, err)
		}
	}
}

func TestRejectBadExtension(t *testing.T) {
	root, _, _ := setupRoot(t)
	if _, _, err := root.OpenReadOnly("notes.txt"); !errors.Is(err, safeopen.ErrBadExtension) {
		t.Fatalf("err = %v, want ErrBadExtension", err)
	}
}

func TestRejectNonRegular(t *testing.T) {
	root, rootDir, _ := setupRoot(t)
	if err := os.Mkdir(filepath.Join(rootDir, "dir.dd"), 0o755); err != nil {
		t.Fatal(err)
	}
	if _, _, err := root.OpenReadOnly("dir.dd"); !errors.Is(err, safeopen.ErrNotRegular) {
		t.Fatalf("err = %v, want ErrNotRegular", err)
	}
}
