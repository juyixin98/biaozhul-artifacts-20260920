// Package digest implements OCI digest parsing and verification.
//
// Digests use the OCI/content-addressable form "<algorithm>:<encoded>".
// All hashes are computed with the real crypto/sha256 and crypto/sha512
// implementations — nothing is trusted from descriptor fields.
package digest

import (
	"bytes"
	"crypto/sha256"
	"crypto/sha512"
	"encoding/hex"
	"errors"
	"fmt"
	"hash"
	"io"
	"strings"
)

// Digest is a parsed, syntactically valid OCI digest.
type Digest struct {
	algorithm string
	encoded   string
}

var (
	// ErrInvalid is returned when a digest string cannot be parsed.
	ErrInvalid = errors.New("invalid digest")
	// ErrMismatch is returned when blob content does not hash to the claimed digest.
	ErrMismatch = errors.New("digest mismatch")
	// ErrSizeMismatch is returned when the byte count differs from the declared size.
	ErrSizeMismatch = errors.New("size mismatch")
)

// Parse parses and validates an OCI digest string.
func Parse(s string) (Digest, error) {
	algo, enc, ok := strings.Cut(s, ":")
	if !ok || algo == "" || enc == "" {
		return Digest{}, fmt.Errorf("%w: %q: missing algorithm or encoded portion", ErrInvalid, s)
	}
	if err := validateAlgorithm(algo); err != nil {
		return Digest{}, err
	}
	if !validEncoded(enc) {
		return Digest{}, fmt.Errorf("%w: %q: bad encoded portion", ErrInvalid, s)
	}
	return Digest{algorithm: algo, encoded: enc}, nil
}

func validateAlgorithm(algo string) error {
	switch algo {
	case "sha256", "sha512":
		return nil
	default:
		return fmt.Errorf("%w: unsupported algorithm %q (want sha256 or sha512)", ErrInvalid, algo)
	}
}

func validEncoded(enc string) bool {
	if len(enc) < 32 {
		return false
	}
	for _, r := range enc {
		switch {
		case r >= 'a' && r <= 'f', r >= '0' && r <= '9':
		default:
			return false
		}
	}
	return true
}

// String renders the canonical "algorithm:encoded" form.
func (d Digest) String() string {
	if d.algorithm == "" {
		return ""
	}
	return d.algorithm + ":" + d.encoded
}

// Algorithm returns the hash algorithm name.
func (d Digest) Algorithm() string { return d.algorithm }

// Encoded returns the hex-encoded hash.
func (d Digest) Encoded() string { return d.encoded }

// IsZero reports whether d is the zero value.
func (d Digest) IsZero() bool { return d.algorithm == "" }

func newHasher(algorithm string) (hash.Hash, error) {
	switch algorithm {
	case "sha256":
		return sha256.New(), nil
	case "sha512":
		return sha512.New(), nil
	default:
		return nil, fmt.Errorf("%w: unsupported algorithm %q", ErrInvalid, algorithm)
	}
}

// FromReader consumes r and returns the digest of its content and byte count.
func FromReader(algorithm string, r io.Reader) (Digest, int64, error) {
	h, err := newHasher(algorithm)
	if err != nil {
		return Digest{}, 0, err
	}
	n, err := io.Copy(h, r)
	if err != nil {
		return Digest{}, 0, fmt.Errorf("hashing content: %w", err)
	}
	return Digest{algorithm: algorithm, encoded: hex.EncodeToString(h.Sum(nil))}, n, nil
}

// FromBytes returns the digest of b using the given algorithm.
func FromBytes(algorithm string, b []byte) (Digest, error) {
	d, _, err := FromReader(algorithm, bytes.NewReader(b))
	return d, err
}

// VerifyReader consumes r and checks both the digest and the declared size of
// its content. It returns the number of bytes read.
func VerifyReader(claimed Digest, declaredSize int64, r io.Reader) (int64, error) {
	h, err := newHasher(claimed.algorithm)
	if err != nil {
		return 0, err
	}
	n, err := io.Copy(h, r)
	if err != nil {
		return 0, fmt.Errorf("reading blob for verification: %w", err)
	}
	got := hex.EncodeToString(h.Sum(nil))
	if got != claimed.encoded {
		return n, fmt.Errorf("%w: expected %s, content hashes to %s:%s", ErrMismatch, claimed, claimed.algorithm, got)
	}
	if declaredSize >= 0 && n != declaredSize {
		return n, fmt.Errorf("%w: descriptor declares %d bytes for %s but blob is %d bytes",
			ErrSizeMismatch, declaredSize, claimed, n)
	}
	return n, nil
}
