package oci

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestParseDigest(t *testing.T) {
	for _, bad := range []string{"", "nocolon", "sha256:short", "md5:" + strings.Repeat("a", 32), "sha256:" + strings.Repeat("z", 64)} {
		if _, _, err := ParseDigest(bad); err == nil {
			t.Errorf("ParseDigest(%q) want error", bad)
		}
	}
	if _, _, err := ParseDigest("sha256:" + strings.Repeat("a", 64)); err != nil {
		t.Errorf("valid digest rejected: %v", err)
	}
}

func TestVerifyBlobDigestMismatch(t *testing.T) {
	dir := t.TempDir()
	content := []byte("hello world")
	p := filepath.Join(dir, "x")
	if err := os.WriteFile(p, content, 0o644); err != nil {
		t.Fatal(err)
	}
	wrong := "sha256:" + strings.Repeat("0", 64)
	if _, err := VerifyBlob(p, wrong, int64(len(content))); err == nil ||
		!strings.Contains(err.Error(), "digest mismatch") {
		t.Fatalf("want digest mismatch, got %v", err)
	}
}

func TestVerifyBlobSizeMismatch(t *testing.T) {
	dir := t.TempDir()
	content := []byte("hello world")
	p := filepath.Join(dir, "x")
	if err := os.WriteFile(p, content, 0o644); err != nil {
		t.Fatal(err)
	}
	d := DigestBytes(content)
	if _, err := VerifyBlob(p, d, int64(len(content)+1)); err == nil ||
		!strings.Contains(err.Error(), "size mismatch") {
		t.Fatalf("want size mismatch, got %v", err)
	}
}

func TestVerifyBlobMissing(t *testing.T) {
	d := DigestBytes([]byte("x"))
	if _, err := VerifyBlob("/nonexistent/blob", d, 1); err == nil {
		t.Fatal("want error for missing blob")
	}
}
