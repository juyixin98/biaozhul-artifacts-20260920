package blob_test

import (
	"os"
	"strings"
	"testing"

	"github.com/example/artifact-promotion/internal/blob"
)

func TestPutVerifyCopy(t *testing.T) {
	s, err := blob.New(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	digest, size, err := s.Put("test", strings.NewReader("hello artifact"))
	if err != nil {
		t.Fatal(err)
	}
	if size != 14 {
		t.Fatalf("size = %d, want 14", size)
	}
	if err := blob.ValidateDigest(digest); err != nil {
		t.Fatalf("digest invalid: %v", err)
	}
	if _, err := s.Verify("test", digest); err != nil {
		t.Fatalf("verify: %v", err)
	}

	// Copy to another env namespace and verify.
	if _, err := s.Copy("test", "staging", digest); err != nil {
		t.Fatalf("copy: %v", err)
	}
	if _, err := s.Verify("staging", digest); err != nil {
		t.Fatalf("verify copy: %v", err)
	}

	// Corrupt the copy: verification must fail.
	p := s.Path("staging", digest)
	if err := os.WriteFile(p, []byte("tampered"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Verify("staging", digest); err == nil {
		t.Fatal("verify should fail on tampered blob")
	}

	// Copying a missing blob must fail.
	if _, err := s.Copy("test", "prod", "sha256:"+strings.Repeat("0", 64)); err == nil {
		t.Fatal("copy of missing blob should fail")
	}
}

func TestValidateDigestRejectsTags(t *testing.T) {
	for _, bad := range []string{"latest", "v1.2.3", "sha256:deadbeef", "sha256:" + strings.Repeat("g", 64)} {
		if err := blob.ValidateDigest(bad); err == nil {
			t.Errorf("digest %q should be rejected", bad)
		}
	}
	good := "sha256:" + strings.Repeat("a", 64)
	if err := blob.ValidateDigest(good); err != nil {
		t.Errorf("digest %q should be accepted: %v", good, err)
	}
}
