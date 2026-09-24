// Package digestx implements the content-addressing digest scheme used by
// OCI Distribution: "sha256:<hex>". All hashing in this project is real
// SHA-256 performed over the exact transmitted bytes.
package digestx

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"hash"
	"io"
	"strings"
)

const AlgorithmSHA256 = "sha256"

var (
	ErrDigestFormat   = errors.New("invalid digest: expected sha256:<64 lowercase hex chars>")
	ErrDigestMismatch = errors.New("digest mismatch")
)

// Parse validates a digest string and returns (algorithm, hex).
func Parse(s string) (string, string, error) {
	algo, enc, ok := strings.Cut(s, ":")
	if !ok || algo != AlgorithmSHA256 {
		return "", "", ErrDigestFormat
	}
	if len(enc) != hex.EncodedLen(sha256.Size) {
		return "", "", ErrDigestFormat
	}
	if _, err := hex.DecodeString(enc); err != nil {
		return "", "", ErrDigestFormat
	}
	if enc != strings.ToLower(enc) {
		return "", "", ErrDigestFormat
	}
	return algo, enc, nil
}

// Valid reports whether s is a well-formed sha256 digest.
func Valid(s string) bool {
	_, _, err := Parse(s)
	return err == nil
}

// FromBytes returns the canonical digest of b.
func FromBytes(b []byte) string {
	sum := sha256.Sum256(b)
	return AlgorithmSHA256 + ":" + hex.EncodeToString(sum[:])
}

// VerifyingWriter hashes everything written to it; Verify checks the result
// against an expected digest.
type VerifyingWriter struct {
	w      io.Writer
	h      hash.Hash
	expect string
	n      int64
}

func NewVerifyingWriter(w io.Writer, expect string) (*VerifyingWriter, error) {
	if _, _, err := Parse(expect); err != nil {
		return nil, err
	}
	return &VerifyingWriter{w: w, h: sha256.New(), expect: expect}, nil
}

func (v *VerifyingWriter) Write(p []byte) (int, error) {
	n, err := v.w.Write(p)
	v.n += int64(n)
	v.h.Write(p[:n])
	return n, err
}

func (v *VerifyingWriter) Bytes() int64 { return v.n }

// Hasher is a streaming tee: everything written is forwarded to w and hashed.
type Hasher struct {
	w io.Writer
	h hash.Hash
	n int64
}

// NewHasher hashes (and forwards to w, which may be io.Discard) all writes.
func NewHasher(w io.Writer) *Hasher {
	return &Hasher{w: w, h: sha256.New()}
}

func (x *Hasher) Write(p []byte) (int, error) {
	n, err := x.w.Write(p)
	x.n += int64(n)
	x.h.Write(p[:n])
	return n, err
}

func (x *Hasher) Bytes() int64 { return x.n }

// Digest returns the digest of everything written so far.
func (x *Hasher) Digest() string {
	return AlgorithmSHA256 + ":" + hex.EncodeToString(x.h.Sum(nil))
}

// Verify returns ErrDigestMismatch when the streamed content does not hash to
// the expected digest.
func (v *VerifyingWriter) Verify() error {
	got := AlgorithmSHA256 + ":" + hex.EncodeToString(v.h.Sum(nil))
	if got != v.expect {
		return fmt.Errorf("%w: expected %s, computed %s", ErrDigestMismatch, v.expect, got)
	}
	return nil
}

// VerifyReaderAll consumes r, hashes it and compares with expect.
func VerifyReaderAll(r io.Reader, expect string) ([]byte, error) {
	b, err := io.ReadAll(r)
	if err != nil {
		return nil, err
	}
	if FromBytes(b) != expect {
		return b, fmt.Errorf("%w: expected %s, computed %s", ErrDigestMismatch, expect, FromBytes(b))
	}
	return b, nil
}

// JSONDigest returns the digest of the canonical-as-transmitted JSON bytes
// (manifests are addressed by their exact wire bytes, per the spec).
func JSONDigest(v any) (string, []byte, error) {
	b, err := json.Marshal(v)
	if err != nil {
		return "", nil, err
	}
	return FromBytes(b), b, nil
}
