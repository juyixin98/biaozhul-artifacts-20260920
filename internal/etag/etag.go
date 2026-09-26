// Package etag implements RFC 9110 entity tags and comparison rules.
//
// Strong tags look like: "v3-9f2a41c0b7e1d55e"
//   - v3     is the resource version (never reused, even across delete/recreate)
//   - suffix is a hash of the representation content
//
// Weak tags carry the W/ prefix: W/"v3-9f2a41c0b7e1d55e". Per RFC 9110
// section 13.1.1 a weak validator MUST NOT drive a state-changing request
// such as PUT or DELETE; only strong comparison is allowed there.
package etag

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"strings"
)

// ErrFormat is returned when a header value is not a valid entity tag.
var ErrFormat = errors.New("malformed entity tag")

// ETag is a parsed entity tag.
type ETag struct {
	Weak   bool
	Opaque string // value inside the quotes, without the W/ prefix
}

// New builds a strong entity tag from a version and representation content.
// Version and content are bound into one token, so they cannot diverge.
func New(version int64, content []byte) ETag {
	sum := sha256.Sum256(content)
	return ETag{Opaque: fmt.Sprintf("v%d-%s", version, hex.EncodeToString(sum[:8]))}
}

// String renders the tag exactly as it appears in an HTTP header.
func (e ETag) String() string {
	if e.Weak {
		return `W/"` + e.Opaque + `"`
	}
	return `"` + e.Opaque + `"`
}

// Parse parses a single entity tag, e.g. W/"v1-ab12" or "v2-cd34".
func Parse(s string) (ETag, error) {
	s = strings.TrimSpace(s)
	weak := false
	if strings.HasPrefix(s, "W/") || strings.HasPrefix(s, "w/") {
		weak = true
		s = s[2:]
	}
	if len(s) < 2 || s[0] != '"' || s[len(s)-1] != '"' {
		return ETag{}, fmt.Errorf("%w: %q", ErrFormat, s)
	}
	return ETag{Weak: weak, Opaque: s[1 : len(s)-1]}, nil
}

// ParseList parses an If-Match header: either "*" (match any existing
// representation) or a comma-separated list of entity tags.
// An empty header yields nil tags, any=false and no error; callers decide
// whether an absent precondition is an error.
func ParseList(header string) (tags []ETag, any bool, err error) {
	header = strings.TrimSpace(header)
	if header == "" {
		return nil, false, nil
	}
	if header == "*" {
		return nil, true, nil
	}
	for _, part := range strings.Split(header, ",") {
		tag, perr := Parse(part)
		if perr != nil {
			return nil, false, perr
		}
		tags = append(tags, tag)
	}
	return tags, false, nil
}

// StrongEqual implements the strong comparison function (RFC 9110 8.8.3.2):
// both tags must be strong and their opaque parts must be identical.
// A weak tag never compares equal under strong comparison.
func StrongEqual(a, b ETag) bool {
	return !a.Weak && !b.Weak && a.Opaque == b.Opaque
}

// WeakEqual implements weak comparison: opaque parts match regardless of the
// weak/strong marking. Provided for completeness; state-changing requests
// MUST NOT use it.
func WeakEqual(a, b ETag) bool {
	return a.Opaque == b.Opaque
}
