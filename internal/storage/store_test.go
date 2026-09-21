package storage

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

var pngHead = []byte{0x89, 'P', 'N', 'G', 0x0D, 0x0A, 0x1A, 0x0A, 1, 2, 3}
var pdfHead = []byte("%PDF-1.7 some bytes after the magic")

func newTestStore(t *testing.T, maxBytes int64) *Store {
	t.Helper()
	dir := t.TempDir()
	s, err := New(dir, maxBytes)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	return s
}

func TestSavePDFAndPNG(t *testing.T) {
	s := newTestStore(t, 1<<20)
	for name, data := range map[string][]byte{"pdf": pdfHead, "png": pngHead} {
		res, err := s.Save(bytes.NewReader(data), 42)
		if err != nil {
			t.Fatalf("%s Save: %v", name, err)
		}
		if res.Kind.Name != name {
			t.Fatalf("%s kind = %s", name, res.Kind.Name)
		}
		if res.Size != int64(len(data)) {
			t.Fatalf("%s size = %d want %d", name, res.Size, len(data))
		}
		want := sha256.Sum256(data)
		if res.SHA256 != hex.EncodeToString(want[:]) {
			t.Fatalf("%s sha mismatch", name)
		}
		// File is readable through Resolve (no traversal) and on disk.
		abs, err := s.Resolve(res.RelPath)
		if err != nil {
			t.Fatalf("Resolve: %v", err)
		}
		if _, err := os.Stat(abs); err != nil {
			t.Fatalf("stat: %v", err)
		}
		if !strings.HasSuffix(res.RelPath, "job-42/") && filepath.Dir(res.RelPath) != "job-42" {
			t.Fatalf("path not sharded under job-42: %s", res.RelPath)
		}
	}
}

func TestSaveRejectsBadMagic(t *testing.T) {
	s := newTestStore(t, 1<<20)
	for _, data := range [][]byte{
		[]byte("MZ\x90\x00binary"),
		[]byte("<!DOCTYPE html><html>"),
		{0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08},
		[]byte("short"), // below minimum magic length
	} {
		if _, err := s.Save(bytes.NewReader(data), 1); !errors.Is(err, ErrUnsupportedKind) {
			t.Fatalf("want ErrUnsupportedKind, got %v", err)
		}
	}
	// No files left behind after a rejected save.
	entries, _ := os.ReadDir(s.absRoot)
	for _, e := range entries {
		if e.IsDir() {
			fs, _ := os.ReadDir(filepath.Join(s.absRoot, e.Name()))
			if len(fs) != 0 {
				t.Fatalf("rejected save left files in %s: %d", e.Name(), len(fs))
			}
		}
	}
}

func TestSaveEnforcesSize(t *testing.T) {
	const max = 100
	s := newTestStore(t, max)
	big := append(append([]byte{}, pdfHead...), make([]byte, max)...) // > max
	_, err := s.Save(bytes.NewReader(big), 1)
	if !errors.Is(err, ErrTooLarge) {
		t.Fatalf("want ErrTooLarge, got %v", err)
	}
	// Exactly at the limit succeeds.
	exact := append(append([]byte{}, pdfHead...), make([]byte, max-len(pdfHead))...)
	if _, err := s.Save(bytes.NewReader(exact), 1); err != nil {
		t.Fatalf("exact-limit save: %v", err)
	}
}

func TestResolveRejectsTraversal(t *testing.T) {
	s := newTestStore(t, 1<<20)
	attacks := []string{
		"../../../etc/passwd",
		"..",
		"../",
		"job-1/../../etc",
		"/etc/passwd",
		"job-1/sub/../../../etc/passwd",
		"job-1/\x00evil",
		"",
	}
	for _, p := range attacks {
		if _, err := s.Resolve(p); err == nil {
			t.Fatalf("traversal path accepted: %q", p)
		}
	}

	// A path that merely normalizes back inside the root is harmless and is
	// accepted (it still cannot address anything outside the root).
	if _, err := s.Resolve("job-1/sub/../../x.pdf"); err != nil {
		t.Fatalf("in-root normalized path rejected: %v", err)
	}
}

func TestDiscardAndSweep(t *testing.T) {
	s := newTestStore(t, 1<<20)
	res, err := s.Save(bytes.NewReader(pdfHead), 1)
	if err != nil {
		t.Fatal(err)
	}
	if err := s.Discard(res.RelPath); err != nil {
		t.Fatalf("discard: %v", err)
	}
	if err := s.Discard(res.RelPath); err != nil {
		t.Fatalf("discard missing file should be nil, got %v", err)
	}
	// Orphan sweep: a committed blob unknown to the DB is removed; known
	// blobs survive.
	keep, err := s.Save(bytes.NewReader(pngHead), 2)
	if err != nil {
		t.Fatal(err)
	}
	gone, err := s.Save(bytes.NewReader(pdfHead), 2)
	if err != nil {
		t.Fatal(err)
	}
	partPath := filepath.Join(s.absRoot, "job-2", "left.part")
	if err := os.WriteFile(partPath, []byte("partial"), 0o640); err != nil {
		t.Fatal(err)
	}
	partials, blobs, err := s.SweepOrphans(map[string]struct{}{keep.RelPath: {}})
	if err != nil {
		t.Fatal(err)
	}
	if partials != 1 {
		t.Fatalf("partials removed = %d, want 1", partials)
	}
	if blobs != 1 {
		t.Fatalf("orphan blobs removed = %d, want 1", blobs)
	}
	if _, err := os.Stat(filepath.Join(s.absRoot, gone.RelPath)); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("orphan blob survived sweep")
	}
	if _, err := os.Stat(filepath.Join(s.absRoot, keep.RelPath)); err != nil {
		t.Fatalf("known blob wrongly removed: %v", err)
	}
}
