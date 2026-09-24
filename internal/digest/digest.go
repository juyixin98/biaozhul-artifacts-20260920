// Package digest implements canonical OCI-style content digests.
// Only sha256 is supported: "sha256:" + lowercase hex(SHA-256(content)).
package digest

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"regexp"
)

const Prefix = "sha256:"

var pattern = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)

// ErrInvalid is returned when a digest string is malformed.
var ErrInvalid = errors.New("invalid digest: expected sha256:<64 lowercase hex chars>")

// Valid reports whether d is a well-formed sha256 digest.
func Valid(d string) bool { return pattern.MatchString(d) }

// Check validates a digest string and returns it unchanged.
func Check(d string) (string, error) {
	if !Valid(d) {
		return "", ErrInvalid
	}
	return d, nil
}

// FromBytes computes the digest of an in-memory byte slice.
func FromBytes(b []byte) string {
	sum := sha256.Sum256(b)
	return Prefix + hex.EncodeToString(sum[:])
}

// FromReader streams data through SHA-256 and returns its digest and byte count.
func FromReader(r io.Reader) (string, int64, error) {
	h := sha256.New()
	n, err := io.Copy(h, r)
	if err != nil {
		return "", 0, fmt.Errorf("hashing stream: %w", err)
	}
	return Prefix + hex.EncodeToString(h.Sum(nil)), n, nil
}
