package digest_test

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"strings"
	"testing"

	"snapshotcontroller/internal/digest"
)

func TestComputeDeterministicAndOrdered(t *testing.T) {
	files := digest.Files{
		"b.txt": []byte("second"),
		"a.txt": []byte("first"),
	}
	r1, err := digest.Compute(files, "")
	if err != nil {
		t.Fatalf("Compute: %v", err)
	}
	r2, err := digest.Compute(files, "")
	if err != nil {
		t.Fatalf("Compute: %v", err)
	}
	if r1.Digest != r2.Digest {
		t.Fatalf("digest not deterministic: %s vs %s", r1.Digest, r2.Digest)
	}
	// Manifest must be byte-sorted regardless of map iteration order.
	lines := strings.Split(strings.TrimSuffix(r1.Manifest, "\n"), "\n")
	if len(lines) != 2 || !strings.HasSuffix(lines[0], "  a.txt") || !strings.HasSuffix(lines[1], "  b.txt") {
		t.Fatalf("manifest not sorted: %q", r1.Manifest)
	}
	if r1.FileCount != 2 || r1.TotalBytes != int64(len("first")+len("second")) {
		t.Fatalf("unexpected counts: %+v", r1)
	}
	if r1.Algorithm != "SHA-256" || len(r1.Digest) != 64 {
		t.Fatalf("unexpected algorithm/digest: %+v", r1)
	}

	// Per-file hashes are sha256 of the raw value and reproducible with
	// coreutils sha256sum semantics.
	sumA := sha256.Sum256([]byte("first"))
	if !strings.HasPrefix(r1.Manifest, hex.EncodeToString(sumA[:])+"  5  a.txt\n") {
		t.Fatalf("manifest line for a.txt wrong: %q", r1.Manifest)
	}

	// Root digest is sha256 over the whole manifest text.
	root := sha256.Sum256([]byte(r1.Manifest))
	if got := hex.EncodeToString(root[:]); got != r1.Digest {
		t.Fatalf("root digest mismatch: got %s want %s", got, r1.Digest)
	}

	// Verify must reproduce the digest and reject any tampering.
	recomputed, err := digest.Verify(r1.Manifest, files)
	if err != nil {
		t.Fatalf("Verify: %v", err)
	}
	if recomputed != r1.Digest {
		t.Fatalf("manifest round-trip mismatch: %s vs %s", recomputed, r1.Digest)
	}

	// Tamper with the per-file hash line: content would no longer match.
	goodHash := hex.EncodeToString(sumA[:])
	badHash := "f" + goodHash[1:]
	tampered := strings.Replace(r1.Manifest, goodHash, badHash, 1)
	if _, err := digest.Verify(tampered, files); err == nil {
		t.Fatal("expected hash-tampered manifest to fail verification")
	}

	// Tamper with a file name in the manifest: key no longer exists.
	renamed := strings.Replace(r1.Manifest, "a.txt", "a-renamed.txt", 1)
	if _, err := digest.Verify(renamed, files); err == nil {
		t.Fatal("expected renamed manifest line to fail verification")
	}
}

func TestComputeSubPath(t *testing.T) {
	files := digest.Files{"a.txt": []byte("first"), "b.txt": []byte("second")}
	got, err := digest.Compute(files, "b.txt")
	if err != nil {
		t.Fatalf("Compute: %v", err)
	}
	want, err := digest.Compute(digest.Files{"b.txt": []byte("second")}, "")
	if err != nil {
		t.Fatalf("Compute: %v", err)
	}
	if got.Digest != want.Digest {
		t.Fatalf("subPath digest mismatch: %s vs %s", got.Digest, want.Digest)
	}

	if _, err := digest.Compute(files, "missing"); !errors.Is(err, digest.ErrFileNotFound) {
		t.Fatalf("expected ErrFileNotFound, got %v", err)
	}
}

func TestEmptySource(t *testing.T) {
	// An empty ConfigMap (no selected keys) yields the SHA-256 of empty input.
	r, err := digest.Compute(digest.Files{}, "")
	if err != nil {
		t.Fatalf("Compute: %v", err)
	}
	sum := sha256.Sum256(nil)
	if r.Digest != hex.EncodeToString(sum[:]) || r.FileCount != 0 || r.TotalBytes != 0 || r.Manifest != "" {
		t.Fatalf("unexpected empty-source result: %+v", r)
	}
}

func TestNameIsBoundViaManifest(t *testing.T) {
	// Same content under different names must produce different digests,
	// because the file name is part of the hashed manifest.
	a, _ := digest.Compute(digest.Files{"one.txt": []byte("payload")}, "")
	b, _ := digest.Compute(digest.Files{"two.txt": []byte("payload")}, "")
	if a.Digest == b.Digest {
		t.Fatal("digest failed to bind the file name")
	}
}
