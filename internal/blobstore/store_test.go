package blobstore_test

import (
	"bytes"
	"errors"
	"io"
	"os"
	"path/filepath"
	"testing"

	"github.example.com/ocimultipick/internal/blobstore"
	"github.example.com/ocimultipick/internal/digest"
	"github.example.com/ocimultipick/internal/oci"
)

func newStore(t *testing.T) *blobstore.Store {
	t.Helper()
	return blobstore.New(t.TempDir())
}

func TestPutHashesRealBytesAndRoundTrips(t *testing.T) {
	s := newStore(t)
	content := []byte("{\"hello\":\"world\"}")
	desc, err := s.PutBytes("repo-a", "sha256", oci.MediaTypeImageConfig, content)
	if err != nil {
		t.Fatal(err)
	}
	// The recorded digest must match an independent hash of the content.
	want, err := digest.FromBytes("sha256", content)
	if err != nil {
		t.Fatal(err)
	}
	if desc.Digest != want.String() {
		t.Fatalf("PutBytes digest %s, want %s", desc.Digest, want)
	}
	if desc.Size != int64(len(content)) {
		t.Fatalf("size %d, want %d", desc.Size, len(content))
	}

	r, size, err := s.Open("repo-a", want)
	if err != nil {
		t.Fatal(err)
	}
	defer r.Close()
	got, _ := io.ReadAll(r)
	if size != int64(len(content)) || !bytes.Equal(got, content) {
		t.Fatalf("round trip mismatch: size=%d", size)
	}
}

func TestVerifyDetectsTamperedOnDiskBlob(t *testing.T) {
	s := newStore(t)
	content := []byte("original manifest bytes")
	desc, err := s.PutBytes("r", "sha256", oci.MediaTypeImageManifest, content)
	if err != nil {
		t.Fatal(err)
	}
	// Tamper the bytes on disk while the descriptor still claims the old digest.
	d, err := digest.Parse(desc.Digest)
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(s.Root(), "r", "blobs", d.Algorithm(), d.Encoded())
	if err := os.WriteFile(path, []byte("tampered bytes on disk"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := s.Verify("r", desc); !errors.Is(err, digest.ErrMismatch) {
		t.Fatalf("Verify after tamper: %v, want digest mismatch", err)
	}
}

func TestVerifyDetectsSizeMismatch(t *testing.T) {
	s := newStore(t)
	desc, err := s.PutBytes("r", "sha256", "", []byte("12345"))
	if err != nil {
		t.Fatal(err)
	}
	desc.Size = 999
	if err := s.Verify("r", desc); !errors.Is(err, digest.ErrSizeMismatch) {
		t.Fatalf("Verify with wrong size: %v, want size mismatch", err)
	}
}

func TestMissingBlob(t *testing.T) {
	s := newStore(t)
	if err := s.EnsureRepo("r"); err != nil {
		t.Fatal(err)
	}
	d, _ := digest.FromBytes("sha256", []byte("x"))
	if _, _, err := s.Open("r", d); !errors.Is(err, blobstore.ErrNotFound) {
		t.Fatalf("Open missing: %v, want ErrNotFound", err)
	}
}

func TestRepoValidationPreventsEscape(t *testing.T) {
	for _, repo := range []string{"../escape", "a/../../b", "/abs", "a//b", "", "BAD"} {
		if blobstore.ValidRepo(repo) {
			t.Errorf("ValidRepo(%q) = true, want false", repo)
		}
	}
	for _, repo := range []string{"a", "a/b", "a/b-c_1.d"} {
		if !blobstore.ValidRepo(repo) {
			t.Errorf("ValidRepo(%q) = false, want true", repo)
		}
	}
}
