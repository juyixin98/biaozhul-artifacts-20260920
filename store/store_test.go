package store

import (
	"bytes"
	"context"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"modelcache/digest"
)

func newTestStore(t *testing.T, maxSize int64) *Store {
	t.Helper()
	dir := t.TempDir()
	st, err := New(Options{Root: dir, MaxObjectSize: maxSize, Now: time.Now})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	return st
}

func TestPutGetRoundTrip(t *testing.T) {
	st := newTestStore(t, 1<<20)
	ctx := context.Background()
	payload := bytes.Repeat([]byte("model-cache-"), 1000)
	dgst := digest.NewSHA256(payload)

	size, existed, err := st.Put(ctx, dgst, int64(len(payload)), bytes.NewReader(payload))
	if err != nil {
		t.Fatalf("Put: %v", err)
	}
	if existed {
		t.Fatal("new object should not be reported as already present")
	}
	if size != int64(len(payload)) {
		t.Fatalf("size = %d, want %d", size, len(payload))
	}

	f, fi, err := st.Get(ctx, dgst)
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	defer f.Close()
	got, _ := io.ReadAll(f)
	if !bytes.Equal(got, payload) {
		t.Fatal("downloaded bytes differ")
	}
	if fi.Mode().Perm() != 0o444 {
		t.Fatalf("published blob perms = %o, want 0444", fi.Mode().Perm())
	}
}

func TestPutIdempotent(t *testing.T) {
	st := newTestStore(t, 1<<20)
	ctx := context.Background()
	payload := []byte("repeatable artifact")
	dgst := digest.NewSHA256(payload)

	if _, existed, err := st.Put(ctx, dgst, -1, bytes.NewReader(payload)); err != nil || existed {
		t.Fatalf("first put: existed=%v err=%v", existed, err)
	}
	if _, existed, err := st.Put(ctx, dgst, -1, bytes.NewReader(payload)); err != nil || !existed {
		t.Fatalf("second put: existed=%v err=%v", existed, err)
	}
}

// TestConcurrentSameDigest is acceptance item #1: many clients uploading the
// same digest concurrently must publish exactly one intact object, with no
// errors caused by the temp-file race.
func TestConcurrentSameDigest(t *testing.T) {
	st := newTestStore(t, 1<<20)
	ctx := context.Background()
	payload := bytes.Repeat([]byte("concurrent!"), 4096)
	dgst := digest.NewSHA256(payload)

	const n = 32
	var wg sync.WaitGroup
	errs := make([]error, n)
	existed := make([]bool, n)
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			_, ex, err := st.Put(ctx, dgst, -1, bytes.NewReader(payload))
			errs[i] = err
			existed[i] = ex
		}(i)
	}
	wg.Wait()
	for i, err := range errs {
		if err != nil {
			t.Fatalf("put %d: %v", i, err)
		}
	}
	firstExisted := 0
	for _, ex := range existed {
		if ex {
			firstExisted++
		}
	}
	if firstExisted != n-1 {
		t.Fatalf("expected exactly 1 creator and %d fast-path hits, got %d hits", n-1, firstExisted)
	}

	blobs, err := st.List(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if len(blobs) != 1 || blobs[0].Digest != dgst {
		t.Fatalf("expected exactly one blob, got %+v", blobs)
	}
	if st.statsOrDie(t).TempFiles != 0 {
		t.Fatal("temp dir should be empty after concurrent uploads settle")
	}
}

// TestPutDigestMismatchQuarantines proves a bad upload is never published and
// is retained for diagnosis in quarantine/.
func TestPutDigestMismatchQuarantines(t *testing.T) {
	st := newTestStore(t, 1<<20)
	ctx := context.Background()
	payload := []byte("real content")
	fake := digest.NewSHA256([]byte("different claimed content"))

	_, _, err := st.Put(ctx, fake, -1, bytes.NewReader(payload))
	if !errors.Is(err, ErrDigestMismatch) {
		t.Fatalf("want ErrDigestMismatch, got %v", err)
	}
	if _, _, err := st.Get(ctx, fake); !errors.Is(err, ErrNotFound) {
		t.Fatalf("bad object must not be published, got err=%v", err)
	}
	blobs, _ := st.List(ctx)
	if len(blobs) != 0 {
		t.Fatalf("blobs must stay empty, got %d", len(blobs))
	}
	if got := st.statsOrDie(t).Quarantined; got != 1 {
		t.Fatalf("quarantined = %d, want 1", got)
	}
}

func TestPutRejectsOversize(t *testing.T) {
	const cap_ = 1024
	st := newTestStore(t, cap_)
	ctx := context.Background()
	payload := bytes.Repeat([]byte("x"), cap_+1)
	dgst := digest.NewSHA256(payload)

	_, _, err := st.Put(ctx, dgst, -1, bytes.NewReader(payload))
	if !errors.Is(err, ErrTooLarge) {
		t.Fatalf("want ErrTooLarge, got %v", err)
	}
	if st.statsOrDie(t).TempFiles != 0 {
		t.Fatal("oversize upload must not leave a temp file")
	}

	// Exactly the cap is allowed.
	edge := bytes.Repeat([]byte("y"), cap_)
	if _, _, err := st.Put(ctx, digest.NewSHA256(edge), -1, bytes.NewReader(edge)); err != nil {
		t.Fatalf("max-sized object rejected: %v", err)
	}
}

func TestPutSizeMismatch(t *testing.T) {
	st := newTestStore(t, 1<<20)
	ctx := context.Background()
	payload := []byte("12345")
	_, _, err := st.Put(ctx, digest.NewSHA256(payload), 99, bytes.NewReader(payload))
	if !errors.Is(err, ErrSizeMismatch) {
		t.Fatalf("want ErrSizeMismatch, got %v", err)
	}
}

// TestSweepRemovesOrphans simulates a crash/interrupted upload leaving a tmp
// file, then verifies startup Sweep removes it while blobs are untouched.
func TestSweepRemovesOrphans(t *testing.T) {
	st := newTestStore(t, 1<<20)
	ctx := context.Background()
	payload := []byte("survivor")
	dgst := digest.NewSHA256(payload)
	if _, _, err := st.Put(ctx, dgst, -1, bytes.NewReader(payload)); err != nil {
		t.Fatal(err)
	}
	orphan := filepath.Join(st.tmpDir, "upload-deadbeef.part")
	if err := os.WriteFile(orphan, []byte("partial"), 0o600); err != nil {
		t.Fatal(err)
	}
	n, err := st.Sweep(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if n != 1 {
		t.Fatalf("swept %d files, want 1", n)
	}
	if _, _, err := st.Get(ctx, dgst); err != nil {
		t.Fatalf("published blob must survive sweep: %v", err)
	}
}

// TestVerifyDetectsCorruptBlob tampers with a published file (simulating bad
// cache copied in by an operator) and asserts Verify diagnoses + quarantines.
func TestVerifyDetectsCorruptBlob(t *testing.T) {
	st := newTestStore(t, 1<<20)
	ctx := context.Background()
	payload := []byte(strings.Repeat("corrupt-me-", 256))
	dgst := digest.NewSHA256(payload)
	if _, _, err := st.Put(ctx, dgst, -1, bytes.NewReader(payload)); err != nil {
		t.Fatal(err)
	}
	// Tamper: published files are 0444; reach in via the computed path.
	p, err := st.digestPath(dgst)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(p, 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(p, []byte("completely different bytes"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := st.Verify(ctx, dgst); !errors.Is(err, ErrCorrupt) {
		t.Fatalf("want ErrCorrupt, got %v", err)
	}
	if _, _, err := st.Get(ctx, dgst); !errors.Is(err, ErrNotFound) {
		t.Fatalf("corrupt blob should be quarantined away from blobs/, get err=%v", err)
	}
	if got := st.statsOrDie(t).Quarantined; got != 1 {
		t.Fatalf("quarantined=%d want 1", got)
	}
}

func TestNotFound(t *testing.T) {
	st := newTestStore(t, 1<<20)
	d := digest.NewSHA256([]byte("missing"))
	if _, _, err := st.Get(context.Background(), d); !errors.Is(err, ErrNotFound) {
		t.Fatalf("want ErrNotFound, got %v", err)
	}
}

// helper kept outside to avoid repeating Stats error handling.
func (s *Store) statsOrDie(t *testing.T) Stats {
	t.Helper()
	st, err := s.Stats(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	return st
}
