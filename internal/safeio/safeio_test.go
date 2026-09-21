package safeio

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func writeFile(t *testing.T, path string, data []byte) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, data, 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestResolveAndHashHappyPath(t *testing.T) {
	root := t.TempDir()
	payload := []byte("forensic-image-payload-0123456789")
	writeFile(t, filepath.Join(root, "a.raw"), payload)

	f, id, err := OpenVerify(root, "a.raw")
	if err != nil {
		t.Fatalf("OpenVerify: %v", err)
	}
	defer f.Close()
	want := sha256.Sum256(payload)
	got, chunks, err := HashInChunks(f, id, HashOptions{ChunkSize: 8})
	if err != nil {
		t.Fatalf("HashInChunks: %v", err)
	}
	if got != hex.EncodeToString(want[:]) {
		t.Fatalf("digest mismatch: got %s want %s", got, hex.EncodeToString(want[:]))
	}
	if len(chunks) != 5 { // 33 bytes / 8 -> 5 chunks
		t.Fatalf("chunk count = %d, want 5", len(chunks))
	}
}

func TestPathTraversalRejected(t *testing.T) {
	root := t.TempDir()
	outside := filepath.Join(t.TempDir(), "secret.raw")
	writeFile(t, outside, []byte("x"))

	for _, name := range []string{
		"../secret.raw",
		"../../etc/passwd",
		"sub/../../secret.raw",
		"/etc/passwd",
		"a/../../../secret.raw",
	} {
		if _, _, err := OpenVerify(root, name); !errors.Is(err, ErrOutsideWhitelist) && !errors.Is(err, ErrIllegalName) {
			t.Errorf("name %q: expected whitelist/illegal error, got %v", name, err)
		}
	}
}

func TestSymlinkEscapeRejected(t *testing.T) {
	root := t.TempDir()
	outside := filepath.Join(t.TempDir(), "outside.raw")
	writeFile(t, outside, []byte("external"))

	// Symlink directly to a file outside the root.
	link := filepath.Join(root, "evil.raw")
	if err := os.Symlink(outside, link); err != nil {
		t.Fatal(err)
	}
	if _, _, err := OpenVerify(root, "evil.raw"); !errors.Is(err, ErrOutsideWhitelist) {
		t.Fatalf("symlink escape: expected ErrOutsideWhitelist, got %v", err)
	}

	// Symlinked subdirectory pointing outside root.
	subdir := filepath.Join(root, "linkdir")
	if err := os.Symlink(filepath.Dir(outside), subdir); err != nil {
		t.Fatal(err)
	}
	if _, _, err := OpenVerify(root, filepath.Join("linkdir", "outside.raw")); !errors.Is(err, ErrOutsideWhitelist) {
		t.Fatalf("symlinked-dir escape: expected ErrOutsideWhitelist, got %v", err)
	}

	// A symlink staying inside root is allowed.
	inside := filepath.Join(root, "inside.raw")
	writeFile(t, inside, []byte("inside"))
	if err := os.Symlink("inside.raw", filepath.Join(root, "alias.raw")); err != nil {
		t.Fatal(err)
	}
	f, _, err := OpenVerify(root, "alias.raw")
	if err != nil {
		t.Fatalf("in-root symlink should resolve, got %v", err)
	}
	f.Close()
}

func TestNonRegularFileRejected(t *testing.T) {
	root := t.TempDir()
	if err := os.Mkdir(filepath.Join(root, "dir.raw"), 0o755); err != nil {
		t.Fatal(err)
	}
	if _, _, err := OpenVerify(root, "dir.raw"); !errors.Is(err, ErrNotRegular) {
		t.Fatalf("directory: expected ErrNotRegular, got %v", err)
	}
	// FIFO
	fifo := filepath.Join(root, "fifo.dd")
	if err := mkfifo(fifo); err != nil {
		t.Fatal(err)
	}
	if _, _, err := OpenVerify(root, "fifo.dd"); !errors.Is(err, ErrNotRegular) {
		t.Fatalf("fifo: expected ErrNotRegular, got %v", err)
	}
}

func TestChangeDuringReadDetected(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, "mut.raw")
	data := make([]byte, 64)
	for i := range data {
		data[i] = byte(i)
	}
	writeFile(t, path, data)

	f, id, err := OpenVerify(root, "mut.raw")
	if err != nil {
		t.Fatal(err)
	}
	_, _, err = HashInChunks(f, id, HashOptions{
		ChunkSize:      16,
		VerifyReadback: true,
		AfterChunk: func(off int64) error {
			if off == 16 {
				// Mutate an already-processed byte through a different fd.
				w, werr := os.OpenFile(path, os.O_WRONLY, 0)
				if werr != nil {
					return werr
				}
				defer w.Close()
				if _, werr := w.WriteAt([]byte{0xFF, 0xFE}, 0); werr != nil {
					return werr
				}
				return w.Sync()
			}
			return nil
		},
	})
	f.Close()
	if !errors.Is(err, ErrFileChanged) {
		t.Fatalf("expected ErrFileChanged, got %v", err)
	}
}

func TestMtimeChangeDetected(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, "ts.raw")
	writeFile(t, path, make([]byte, 64))

	f, id, err := OpenVerify(root, "ts.raw")
	if err != nil {
		t.Fatal(err)
	}
	_, _, err = HashInChunks(f, id, HashOptions{
		ChunkSize: 16,
		AfterChunk: func(off int64) error {
			if off == 16 {
				// Content untouched, mtime explicitly moved: stat catches it.
				future := time.Now().Add(time.Hour)
				return os.Chtimes(path, future, future)
			}
			return nil
		},
	})
	f.Close()
	if !errors.Is(err, ErrFileChanged) {
		t.Fatalf("expected ErrFileChanged on mtime change, got %v", err)
	}
}

func TestTruncationDuringReadDetected(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, "shrink.raw")
	writeFile(t, path, make([]byte, 64))

	f, id, err := OpenVerify(root, "shrink.raw")
	if err != nil {
		t.Fatal(err)
	}
	_, _, err = HashInChunks(f, id, HashOptions{
		ChunkSize: 16,
		AfterChunk: func(off int64) error {
			if off == 16 {
				return os.Truncate(path, 8) // shrinks below current offset
			}
			return nil
		},
	})
	f.Close()
	if !errors.Is(err, ErrFileChanged) {
		t.Fatalf("expected ErrFileChanged on truncation, got %v", err)
	}
}

func TestFileOpenedReadOnly(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, "ro.raw")
	writeFile(t, path, []byte("abc"))
	// Remove write bits even for the owner; OpenVerify must still succeed.
	if err := os.Chmod(path, 0o400); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chmod(path, 0o644) })
	f, id, err := OpenVerify(root, "ro.raw")
	if err != nil {
		t.Fatalf("read-only file open: %v", err)
	}
	defer f.Close()
	if _, err := f.Write([]byte("x")); err == nil {
		t.Fatal("evidence descriptor must not be writable")
	}
	if _, _, err := HashInChunks(f, id, HashOptions{ChunkSize: 4}); err != nil {
		t.Fatalf("hash: %v", err)
	}
}
