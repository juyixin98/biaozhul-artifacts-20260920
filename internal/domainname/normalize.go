// Package domainname canonicalizes domain names so that every spelling of
// the "same" name maps to a single storage key.
//
// Normalization rules:
//  1. surrounding whitespace is trimmed;
//  2. trailing dot(s) are stripped — the absolute DNS form "example.com."
//     is the same name as "example.com";
//  3. ASCII case is folded to lower — DNS is case-insensitive, so
//     "ExAmPle.COM" == "example.com";
//  4. internationalized labels are converted to their punycode A-label via
//     IDNA ToASCII — "münchen.de" is stored and compared as
//     "xn--mnchen-3ya.de", so the Unicode and punycode spellings collide
//     on the same unique key.
package domainname

import (
	"errors"
	"fmt"
	"regexp"
	"strings"

	"golang.org/x/net/idna"
)

var (
	ErrEmpty   = errors.New("domain name is empty")
	ErrTooLong = errors.New("domain name exceeds 253 characters")
	ErrNoTLD   = errors.New("domain name must contain at least two labels")

	// LDH label: letters, digits, hyphens; no leading/trailing hyphen; 1-63 chars.
	labelRE = regexp.MustCompile(`^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$`)
)

// Normalize returns the canonical storage form of raw.
func Normalize(raw string) (string, error) {
	s := strings.TrimSpace(raw)
	s = strings.TrimRight(s, ".")
	if s == "" {
		return "", ErrEmpty
	}
	s = strings.ToLower(s)
	ascii, err := idna.ToASCII(s)
	if err != nil {
		return "", fmt.Errorf("invalid internationalized domain name: %w", err)
	}
	if len(ascii) > 253 {
		return "", ErrTooLong
	}
	labels := strings.Split(ascii, ".")
	if len(labels) < 2 {
		return "", ErrNoTLD
	}
	for _, l := range labels {
		if !labelRE.MatchString(l) {
			return "", fmt.Errorf("invalid label %q", l)
		}
	}
	return ascii, nil
}

// TLD returns the right-most label of an already-normalized name.
func TLD(normalized string) string {
	i := strings.LastIndexByte(normalized, '.')
	return normalized[i+1:]
}
