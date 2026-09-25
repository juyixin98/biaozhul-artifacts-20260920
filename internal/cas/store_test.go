package cas

import (
	"bytes"
	"crypto/rand"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
)

func openTestStore(t *testing.T, root string, maxSize int64) *Store {
	t.Helper()
	s, err := Open(root, maxSize)
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	return s
}

func TestPutGetRoundtrip(t *testing.T) {
	s := openTestStore(t, t.TempDir(), 1<<20)
	data := []byte("hello cache")
	digest := DigestOf(data)

	n, err := s.Put(digest, bytes.NewReader(data))
	if err != nil {
		t.Fatalf("Put: %v", err)
	}
	if n != int64(len(data)) {
		t.Fatalf("Put size = %d, want %d", n, len(data))
	}
	if size, ok := s.Has(digest); !ok || size != int64(len(data)) {
		t.Fatalf("Has = %d,%v", size, ok)
	}
	rc, size, err := s.Get(digest)
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	defer rc.Close()
	got, _ := io.ReadAll(rc)
	if !bytes.Equal(got, data) || size != int64(len(data)) {
		t.Fatalf("Get content mismatch")
	}
}

func TestPutRejectsBadInput(t *testing.T) {
	s := openTestStore(t, t.TempDir(), 1<<20)

	if _, err := s.Put("not-a-digest", strings.NewReader("x")); !errors.Is(err, ErrInvalidDigest) {
		t.Fatalf("invalid digest: got %v", err)
	}

	good := DigestOf([]byte("real content"))
	if _, err := s.Put(good, strings.NewReader("different content")); !errors.Is(err, ErrDigestMismatch) {
		t.Fatalf("digest mismatch: got %v", err)
	}
	if _, ok := s.Has(good); ok {
		t.Fatal("mismatched upload must not be published")
	}
}

func TestPutEnforcesSizeLimit(t *testing.T) {
	s := openTestStore(t, t.TempDir(), 4)
	data := []byte("12345") // one byte over the limit
	digest := DigestOf(data)
	if _, err := s.Put(digest, bytes.NewReader(data)); !errors.Is(err, ErrTooLarge) {
		t.Fatalf("oversize: got %v", err)
	}
	if _, ok := s.Has(digest); ok {
		t.Fatal("oversize upload must not be published")
	}
}

func TestPutAbortedReaderPublishesNothing(t *testing.T) {
	s := openTestStore(t, t.TempDir(), 1<<20)
	data := []byte("partial content that never finishes")
	digest := DigestOf(data)
	broken := &failAfter{data: data[:7], err: io.ErrUnexpectedEOF}
	if _, err := s.Put(digest, broken); err == nil {
		t.Fatal("expected error from broken reader")
	}
	if _, ok := s.Has(digest); ok {
		t.Fatal("aborted upload must not be published")
	}
}

// failAfter yields data and then fails with err, simulating an upload
// interrupted mid-stream.
type failAfter struct {
	data []byte
	err  error
}

func (f *failAfter) Read(p []byte) (int, error) {
	if len(f.data) == 0 {
		return 0, f.err
	}
	n := copy(p, f.data)
	f.data = f.data[n:]
	return n, nil
}

// Acceptance: many goroutines uploading the same digest concurrently must
// all succeed, and exactly one intact object must be published.
func TestConcurrentSameDigestUploads(t *testing.T) {
	s := openTestStore(t, t.TempDir(), 8<<20)

	data := make([]byte, 1<<20)
	if _, err := rand.Read(data); err != nil {
		t.Fatal(err)
	}
	digest := DigestOf(data)

	const writers = 32
	var wg sync.WaitGroup
	errs := make([]error, writers)
	for i := 0; i < writers; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			_, errs[i] = s.Put(digest, bytes.NewReader(data))
		}(i)
	}
	wg.Wait()
	for i, err := range errs {
		if err != nil {
			t.Fatalf("writer %d: %v", i, err)
		}
	}

	rc, _, err := s.Get(digest)
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	got, _ := io.ReadAll(rc)
	rc.Close()
	if !bytes.Equal(got, data) {
		t.Fatal("published object content differs from uploaded bytes")
	}
	if objects, _ := s.Stats(); objects != 1 {
		t.Fatalf("objects = %d, want exactly 1", objects)
	}
	// No temp files may survive a completed upload.
	tmpEntries, _ := os.ReadDir(filepath.Join(s.root, "tmp"))
	if len(tmpEntries) != 0 {
		t.Fatalf("leftover tmp entries: %v", tmpEntries)
	}
}

// Acceptance: a restart must preserve published objects, must clean up
// interrupted uploads, and must never expose a partial object.
func TestRestartPersistenceAndTmpCleanup(t *testing.T) {
	root := t.TempDir()

	s1 := openTestStore(t, root, 1<<20)
	data := []byte("durable bytes")
	digest := DigestOf(data)
	if _, err := s1.Put(digest, bytes.NewReader(data)); err != nil {
		t.Fatalf("Put: %v", err)
	}

	// Simulate an upload interrupted by a crash: a half-written temp file
	// whose digest was never published.
	interrupted := DigestOf([]byte("never fully uploaded"))
	if err := os.WriteFile(filepath.Join(root, "tmp", "upload-123456"), []byte("partial"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, ok := s1.Has(interrupted); ok {
		t.Fatal("interrupted upload must not be visible before restart")
	}

	// "Restart": reopen the store on the same root.
	s2 := openTestStore(t, root, 1<<20)

	rc, _, err := s2.Get(digest)
	if err != nil {
		t.Fatalf("published object must survive restart: %v", err)
	}
	got, _ := io.ReadAll(rc)
	rc.Close()
	if !bytes.Equal(got, data) {
		t.Fatal("content changed across restart")
	}

	if _, ok := s2.Has(interrupted); ok {
		t.Fatal("interrupted upload must not be visible after restart")
	}
	tmpEntries, _ := os.ReadDir(filepath.Join(root, "tmp"))
	if len(tmpEntries) != 0 {
		t.Fatalf("tmp dir not cleaned on restart: %v", tmpEntries)
	}
}

// Acceptance: a corrupted cache must be diagnosable via Fsck.
func TestFsckDiagnosesCorruption(t *testing.T) {
	root := t.TempDir()
	s := openTestStore(t, root, 1<<20)

	good := DigestOf([]byte("good"))
	bad := DigestOf([]byte("bad"))
	if _, err := s.Put(good, bytes.NewReader([]byte("good"))); err != nil {
		t.Fatalf("Put good: %v", err)
	}
	if _, err := s.Put(bad, bytes.NewReader([]byte("bad"))); err != nil {
		t.Fatalf("Put bad: %v", err)
	}

	// Corrupt one published object on disk (objects are read-only, so
	// restore permissions first).
	badPath := filepath.Join(root, "objects", bad)
	if err := os.Chmod(badPath, 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(badPath, []byte("tampered"), 0o644); err != nil {
		t.Fatal(err)
	}
	// Drop a leftover temp upload and a foreign file.
	os.WriteFile(filepath.Join(root, "tmp", "upload-stale"), []byte("x"), 0o644)
	os.WriteFile(filepath.Join(root, "stray-file"), []byte("x"), 0o644)

	rep, err := s.Fsck()
	if err != nil {
		t.Fatalf("Fsck: %v", err)
	}
	if rep.OK {
		t.Fatal("Fsck must report a damaged cache as not OK")
	}
	if rep.ObjectsChecked != 2 {
		t.Fatalf("ObjectsChecked = %d, want 2", rep.ObjectsChecked)
	}
	if len(rep.Corrupt) != 1 || !strings.HasPrefix(rep.Corrupt[0], bad) {
		t.Fatalf("Corrupt = %v, want the tampered object", rep.Corrupt)
	}
	if len(rep.LeftoverTmp) != 1 {
		t.Fatalf("LeftoverTmp = %v", rep.LeftoverTmp)
	}
	if len(rep.Unknown) != 1 || rep.Unknown[0] != "stray-file" {
		t.Fatalf("Unknown = %v", rep.Unknown)
	}
}
