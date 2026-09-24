package oci

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"os"
	"strings"
)

// ParseDigest splits "sha256:<hex>" and validates the hex part.
func ParseDigest(d string) (algo, hexPart string, err error) {
	algo, hexPart, ok := strings.Cut(d, ":")
	if !ok || algo == "" || hexPart == "" {
		return "", "", fmt.Errorf("malformed digest %q", d)
	}
	if algo != "sha256" {
		return "", "", fmt.Errorf("unsupported digest algorithm %q in %q", algo, d)
	}
	if len(hexPart) != sha256.Size*2 {
		return "", "", fmt.Errorf("digest %q: bad hex length %d", d, len(hexPart))
	}
	if _, err := hex.DecodeString(hexPart); err != nil {
		return "", "", fmt.Errorf("digest %q: invalid hex: %w", d, err)
	}
	return algo, hexPart, nil
}

// DigestBytes computes the canonical digest of content.
func DigestBytes(b []byte) string {
	sum := sha256.Sum256(b)
	return "sha256:" + hex.EncodeToString(sum[:])
}

// VerifyBlob checks that the file at path exists, that its sha256 equals
// wantDigest and that its size equals wantSize. It returns the actual size.
// The hash is computed by streaming the file; nothing is faked or skipped.
func VerifyBlob(path, wantDigest string, wantSize int64) (int64, error) {
	if _, _, err := ParseDigest(wantDigest); err != nil {
		return 0, err
	}
	f, err := os.Open(path)
	if err != nil {
		return 0, fmt.Errorf("open blob for %s: %w", wantDigest, err)
	}
	defer f.Close()

	h := sha256.New()
	n, err := io.Copy(h, f)
	if err != nil {
		return 0, fmt.Errorf("read blob for %s: %w", wantDigest, err)
	}
	got := "sha256:" + hex.EncodeToString(h.Sum(nil))
	if got != wantDigest {
		return n, fmt.Errorf("digest mismatch: want %s, got %s", wantDigest, got)
	}
	if n != wantSize {
		return n, fmt.Errorf("size mismatch for %s: descriptor says %d bytes, blob has %d bytes", wantDigest, wantSize, n)
	}
	return n, nil
}
