package storage

import (
	"bytes"
	"io"
	"path/filepath"
	"strings"
	"testing"

	"layer-gc/internal/digest"
)

// Verify the upload→verify→publish lifecycle: bytes live in tmp and never
// reach the CAS until their digest is actually confirmed.
func TestStageVerifyPublish(t *testing.T) {
	s, err := New(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	content := bytes.Repeat([]byte("staged-content-"), 100)
	want := digest.FromBytes(content)

	u, err := s.BeginUpload()
	if err != nil {
		t.Fatal(err)
	}
	if _, err := u.ReadFrom(bytes.NewReader(content)); err != nil {
		t.Fatal(err)
	}

	// Not in CAS yet.
	if s.Exists(want) {
		t.Fatal("blob appeared in CAS before verification")
	}

	n, err := s.Verify(u, want)
	if err != nil {
		t.Fatalf("verify: %v", err)
	}
	if n != int64(len(content)) {
		t.Fatalf("verified size %d != %d", n, len(content))
	}
	if err := s.Publish(NewUpload(u.ID, u.Path()), want); err != nil {
		t.Fatalf("publish: %v", err)
	}
	if !s.Exists(want) {
		t.Fatal("published blob missing")
	}
	// Sharded layout: blobs/sha256/<ab>/<digest>.
	if _, err := s.Stat(want); err != nil {
		t.Fatal(err)
	}
	f, err := s.Open(want)
	if err != nil {
		t.Fatal(err)
	}
	got, _ := io.ReadAll(f)
	f.Close()
	if !bytes.Equal(got, content) {
		t.Fatal("published content differs from staged content")
	}
	// Temp name consumed.
	if _, err := s.OpenUpload(u.ID); err == nil {
		t.Fatal("staged temp file survived publish")
	}
}

func TestVerifyRejectsTamperedContent(t *testing.T) {
	s, _ := New(t.TempDir())
	u, err := s.BeginUpload()
	if err != nil {
		t.Fatal(err)
	}
	u.ReadFrom(strings.NewReader("real-content"))

	fake := digest.FromBytes([]byte("different-content"))
	if _, err := s.Verify(u, fake); err == nil {
		t.Fatal("expected digest mismatch error")
	}
	// Failed verification must remove the temp file.
	if _, err := s.OpenUpload(u.ID); err == nil {
		t.Fatal("temp file should be removed after failed verification")
	}
	if s.Exists(fake) {
		t.Fatal("tampered content leaked into CAS")
	}
}

func TestPublishIdempotent(t *testing.T) {
	s, _ := New(t.TempDir())
	content := []byte("identical-content")
	want := digest.FromBytes(content)

	for i := 0; i < 3; i++ {
		u, _ := s.BeginUpload()
		u.ReadFrom(bytes.NewReader(content))
		if _, err := s.Verify(u, want); err != nil {
			t.Fatal(err)
		}
		if err := s.Publish(NewUpload(u.ID, u.Path()), want); err != nil {
			t.Fatalf("publish #%d: %v", i, err)
		}
	}
	blobs, err := s.PublishedBlobs()
	if err != nil {
		t.Fatal(err)
	}
	if len(blobs) != 1 || blobs[0] != want {
		t.Fatalf("expected exactly one CAS blob, got %v", blobs)
	}
}

func TestDiscardAndDelete(t *testing.T) {
	s, _ := New(t.TempDir())
	u, _ := s.BeginUpload()
	u.ReadFrom(strings.NewReader("partial"))
	if err := s.DiscardUpload(u.ID); err != nil {
		t.Fatal(err)
	}
	if _, err := s.OpenUpload(u.ID); err == nil {
		t.Fatal("discard left temp file behind")
	}

	d := digest.FromBytes([]byte("to-be-deleted"))
	u2, _ := s.BeginUpload()
	u2.ReadFrom(strings.NewReader("to-be-deleted"))
	s.Verify(u2, d)
	s.Publish(NewUpload(u2.ID, u2.Path()), d)
	if err := s.Delete(d); err != nil {
		t.Fatal(err)
	}
	if s.Exists(d) {
		t.Fatal("delete did not remove blob")
	}
	if err := s.Delete(d); err != nil {
		t.Fatalf("deleting absent blob must be idempotent, got %v", err)
	}
}

func TestStagedFilesListing(t *testing.T) {
	s, _ := New(t.TempDir())
	for i := 0; i < 3; i++ {
		u, _ := s.BeginUpload()
		u.ReadFrom(strings.NewReader("x"))
		u.Close()
	}
	files, err := s.StagedFiles()
	if err != nil {
		t.Fatal(err)
	}
	if len(files) != 3 {
		t.Fatalf("expected 3 staged files, got %d", len(files))
	}
	for _, f := range files {
		if filepath.Dir(f) != s.TmpDir() {
			t.Fatalf("staged file outside tmp dir: %s", f)
		}
	}
}
