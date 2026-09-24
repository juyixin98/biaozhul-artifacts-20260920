package storage

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io"
	"os"
	"path/filepath"
	"sort"
	"testing"

	"layerregistry/internal/digestx"
)

func digestOf(b []byte) string {
	sum := sha256.Sum256(b)
	return "sha256:" + hex.EncodeToString(sum[:])
}

func newTestStore(t *testing.T) *Store {
	t.Helper()
	dir := t.TempDir()
	s, err := New(dir)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	return s
}

func TestPublishTempVerifiesDigest(t *testing.T) {
	s := newTestStore(t)
	content := []byte("content-addressable layer bytes")
	dg := digestOf(content)
	if err := os.WriteFile(s.TempPath("up1"), content, 0o644); err != nil {
		t.Fatal(err)
	}
	n, err := s.PublishTemp("up1", dg)
	if err != nil {
		t.Fatalf("PublishTemp: %v", err)
	}
	if n != int64(len(content)) {
		t.Fatalf("size = %d, want %d", n, len(content))
	}
	if !s.BlobExists(dg) {
		t.Fatal("published blob missing from CAS tree")
	}
	got, err := os.ReadFile(s.blobPath(dg))
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got, content) {
		t.Fatal("published bytes mismatch")
	}
}

func TestPublishTempRejectsMismatch(t *testing.T) {
	s := newTestStore(t)
	content := []byte("real content")
	wrong := digestOf([]byte("different"))
	if err := os.WriteFile(s.TempPath("up2"), content, 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := s.PublishTemp("up2", wrong); !errors.Is(err, digestx.ErrDigestMismatch) {
		t.Fatalf("want ErrDigestMismatch, got %v", err)
	}
	if s.BlobExists(wrong) {
		t.Fatal("mismatched blob must never be published")
	}
	if _, err := os.Stat(s.TempPath("up2")); err != nil {
		t.Fatal("rejected temp file should remain for inspection/cleanup")
	}
}

func TestStreamToTempHashesRealBytes(t *testing.T) {
	s := newTestStore(t)
	content := bytes.Repeat([]byte("stream-"), 1000)
	n, got, err := s.StreamToTemp("up3", bytes.NewReader(content))
	if err != nil {
		t.Fatal(err)
	}
	if n != int64(len(content)) || got != digestOf(content) {
		t.Fatalf("n=%d digest-match=%v", n, got == digestOf(content))
	}
}

func TestChunkedAppendOffsetEnforced(t *testing.T) {
	s := newTestStore(t)
	_, _ = s.CreateTemp("up4")
	if _, err := s.AppendChunk("up4", 0, bytes.NewReader([]byte("aaaa"))); err != nil {
		t.Fatal(err)
	}
	if _, err := s.AppendChunk("up4", 4, bytes.NewReader([]byte("bbbb"))); err != nil {
		t.Fatalf("sequential append at offset 4: %v", err)
	}
	if _, err := s.AppendChunk("up4", 0, bytes.NewReader([]byte("zz"))); err == nil {
		t.Fatal("append at wrong offset must fail")
	}
	dg := digestOf([]byte("aaaabbbb"))
	if _, err := s.PublishTemp("up4", dg); err != nil {
		t.Fatalf("publish assembled chunks: %v", err)
	}
}

func TestQuarantineRestorePurgeLifecycle(t *testing.T) {
	s := newTestStore(t)
	content := []byte("quarantine me")
	dg := digestOf(content)
	_ = os.WriteFile(s.TempPath("up5"), content, 0o644)
	if _, err := s.PublishTemp("up5", dg); err != nil {
		t.Fatal(err)
	}
	run := "runXYZ"
	if err := s.Quarantine(run, dg); err != nil {
		t.Fatalf("Quarantine: %v", err)
	}
	if s.BlobExists(dg) {
		t.Fatal("blob should be gone from CAS after quarantine")
	}
	// Idempotent quarantine (resumed sweep) must not error.
	if err := s.Quarantine(run, dg); err != nil {
		t.Fatalf("second Quarantine: %v", err)
	}
	if err := s.Restore(run, dg); err != nil {
		t.Fatalf("Restore: %v", err)
	}
	if !s.BlobExists(dg) {
		t.Fatal("blob should be restored to CAS")
	}
	// Now quarantine again and purge.
	if err := s.Quarantine(run, dg); err != nil {
		t.Fatal(err)
	}
	if err := s.Purge(run, dg); err != nil {
		t.Fatalf("Purge: %v", err)
	}
	qd, _ := s.ListQuarantine()
	if len(qd[run]) != 0 {
		t.Fatalf("quarantine should be empty after purge, got %v", qd[run])
	}
}

func TestListTempAndOrphanCleanup(t *testing.T) {
	s := newTestStore(t)
	names := []string{"a", "b", "junk"}
	for _, n := range names {
		if err := os.WriteFile(filepath.Join(s.TempDir(), n), []byte(n), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	got, err := s.ListTemp()
	if err != nil {
		t.Fatal(err)
	}
	sort.Strings(got)
	if len(got) != 3 || got[0] != "a" {
		t.Fatalf("ListTemp = %v", got)
	}
	for _, n := range names {
		if err := s.RemoveTemp(n); err != nil {
			t.Fatal(err)
		}
	}
	got2, _ := s.ListTemp()
	if len(got2) != 0 {
		t.Fatalf("temp dir should be empty, got %v", got2)
	}
}

func TestVerifyBlobFileDetectsCorruption(t *testing.T) {
	s := newTestStore(t)
	content := []byte("named by one hash")
	dg := digestOf(content)
	if err := os.MkdirAll(filepath.Dir(s.blobPath(dg)), 0o755); err != nil {
		t.Fatal(err)
	}
	// Honest publish.
	f, _ := os.Create(s.TempPath("v"))
	_, _ = io.Copy(f, bytes.NewReader(content))
	f.Close()
	if _, err := s.PublishTemp("v", dg); err != nil {
		t.Fatal(err)
	}
	if _, err := s.VerifyBlobFile(dg); err != nil {
		t.Fatalf("honest file: %v", err)
	}
	// Corrupt it in place.
	if err := os.WriteFile(s.blobPath(dg), []byte("tampered bytes!!"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := s.VerifyBlobFile(dg); !errors.Is(err, digestx.ErrDigestMismatch) {
		t.Fatalf("want mismatch for corrupted file, got %v", err)
	}
}
