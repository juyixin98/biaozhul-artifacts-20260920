// Package digest defines content digests used for content-addressed artifacts.
//
// The canonical textual form is "sha256:<64 lowercase hex chars>", e.g.
//
//	sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
package digest

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"strings"
)

// AlgorithmSHA256 is the only supported digest algorithm.
const AlgorithmSHA256 = "sha256"

// Digest is an immutable content digest.
type Digest struct {
	algo string
	enc  string // hex-encoded digest value
}

// NewSHA256 returns the SHA-256 digest of b.
func NewSHA256(b []byte) Digest {
	sum := sha256.Sum256(b)
	return Digest{algo: AlgorithmSHA256, enc: hex.EncodeToString(sum[:])}
}

// FromReader copies all of r through a SHA-256 hasher and returns the digest
// together with the number of bytes consumed.
func FromReader(r io.Reader) (Digest, int64, error) {
	h := sha256.New()
	n, err := io.Copy(h, r)
	if err != nil {
		return Digest{}, n, err
	}
	return Digest{algo: AlgorithmSHA256, enc: hex.EncodeToString(h.Sum(nil))}, n, nil
}

// Hasher returns a fresh SHA-256 hasher.
func Hasher() io.Writer { return sha256.New() }

// Parse parses the canonical "algorithm:hex" textual form.
func Parse(s string) (Digest, error) {
	algo, enc, ok := strings.Cut(s, ":")
	if !ok {
		return Digest{}, fmt.Errorf("digest %q: missing ':' separator", s)
	}
	if algo != AlgorithmSHA256 {
		return Digest{}, fmt.Errorf("digest %q: unsupported algorithm %q (only %s supported)", s, algo, AlgorithmSHA256)
	}
	if len(enc) != sha256.Size*2 {
		return Digest{}, fmt.Errorf("digest %q: expected %d hex chars, got %d", s, sha256.Size*2, len(enc))
	}
	if _, err := hex.DecodeString(enc); err != nil {
		return Digest{}, fmt.Errorf("digest %q: invalid hex: %w", s, err)
	}
	return Digest{algo: algo, enc: strings.ToLower(enc)}, nil
}

// Algorithm returns the algorithm name ("sha256").
func (d Digest) Algorithm() string { return d.algo }

// Hex returns the lowercase hex-encoded digest value.
func (d Digest) Hex() string { return d.enc }

// String returns the canonical "algorithm:hex" form.
func (d Digest) String() string {
	if d.enc == "" {
		return ""
	}
	return d.algo + ":" + d.enc
}

// IsZero reports whether d is the zero value.
func (d Digest) IsZero() bool { return d.enc == "" }

// MarshalText implements encoding.TextMarshaler.
func (d Digest) MarshalText() ([]byte, error) { return []byte(d.String()), nil }

// UnmarshalText implements encoding.TextMarshaler so Digest can be used
// directly in JSON request/response structs.
func (d *Digest) UnmarshalText(b []byte) error {
	parsed, err := Parse(string(b))
	if err != nil {
		return err
	}
	*d = parsed
	return nil
}
