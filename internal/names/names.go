// Package names implements domain-name canonicalization.
//
// Uniqueness is enforced on the canonical form. Canonicalization rules:
//
//  1. Leading/trailing whitespace is trimmed.
//  2. A single trailing dot (the DNS root label, e.g. "Example.COM.") is
//     stripped. More than one trailing dot is rejected.
//  3. Names are case-insensitive: ASCII letters are folded to lower case
//     ("Example.COM" and "example.com" are the same name).
//  4. Internationalized names are normalized with Unicode NFC and converted to
//     ASCII-compatible encoding (Punycode) using the IDNA2008 Registration
//     profile ("bücher.example" -> "xn--bcher-kva.example"). The stored,
//     compared and billed name is the A-label form.
//  5. Each label must be 1-63 characters; the full name at most 253; at least
//     two labels (a name and a TLD) are required; no empty labels.
package names

import (
	"errors"
	"strings"

	"golang.org/x/net/idna"
)

var (
	ErrInvalidName = errors.New("invalid domain name")
)

// registration strictly validates and emits A-labels using IDNA2008
// registration rules. Case folding is performed beforehand (see Normalize),
// because x/net's registration profile validates (and rejects upper case)
// without mapping.
var registration = idna.New(
	idna.ValidateForRegistration(),
)

// Normalize returns the canonical (lower-case, trailing-dot-free, A-label) form.
func Normalize(input string) (string, error) {
	s := strings.TrimSpace(input)
	if s == "" {
		return "", ErrInvalidName
	}
	// Strip exactly one trailing dot; reject names that still end with a dot.
	if strings.HasSuffix(s, ".") {
		s = strings.TrimSuffix(s, ".")
		if s == "" || strings.HasSuffix(s, ".") {
			return "", ErrInvalidName
		}
	}
	if strings.ContainsAny(s, " \t") {
		return "", ErrInvalidName
	}
	// Case-insensitivity: Unicode-aware lower-casing (also folds ASCII) is
	// applied before the strict IDNA2008 registration profile validates and
	// Punycode-encodes the labels.
	s = strings.ToLower(s)
	// IDNA registration profile: validates and converts Unicode labels to
	// A-labels (Punycode), NFC-normalized.
	ascii, err := registration.ToASCII(s)
	if err != nil {
		return "", ErrInvalidName
	}
	labels := strings.Split(ascii, ".")
	if len(labels) < 2 {
		return "", ErrInvalidName
	}
	total := len(labels) - 1 // dots
	for _, l := range labels {
		if l == "" || len(l) > 63 {
			return "", ErrInvalidName
		}
		total += len(l)
	}
	if total > 253 {
		return "", ErrInvalidName
	}
	return ascii, nil
}

// TLD returns the lower-cased final label of a canonical name ("example.com"
// -> "com"). Input is normalized first.
func TLD(input string) (string, error) {
	n, err := Normalize(input)
	if err != nil {
		return "", err
	}
	return n[strings.LastIndex(n, ".")+1:], nil
}
