// Package cluster implements bounded online clustering of raw log lines
// into templates. Variables such as numbers and UUIDs are masked, while
// literal keyword differences are always preserved (two lines that differ
// in a literal word are never merged).
package cluster

import (
	"regexp"
	"strings"
	"unicode/utf8"
)

// Masked variable tokens. Only these tokens (exact match) are considered
// "variables" for template generalization.
const (
	VarNum       = "<NUM>"
	VarUUID      = "<UUID>"
	VarHex       = "<HEX>"
	VarIP        = "<IP>"
	VarStr       = "<STR>"
	VarTruncated = "<TRUNC>" // long-line tail was dropped
)

var (
	uuidRe = regexp.MustCompile(`^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$`)
	ipRe   = regexp.MustCompile(`^\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?$`)
	// 0x-prefixed hex, or bare hex runs of at least 8 chars containing a digit.
	hex0xRe = regexp.MustCompile(`^0x[0-9a-fA-F]+$`)
	hexRe   = regexp.MustCompile(`^[0-9a-fA-F]{8,}$`)
	digitRe = regexp.MustCompile(`[0-9]`)
	// Numbers with optional thousands separators, decimal part and common units.
	numRe = regexp.MustCompile(`^[+-]?[0-9][0-9,]*(?:\.[0-9]+)?(?:ns|us|ms|s|m|h|KB|MB|GB|TB|ki?b|mi?b|gi?b|%)?$`)
	// key=value pairs: the key is preserved, the value is always masked.
	kvRe = regexp.MustCompile(`^([A-Za-z_][A-Za-z0-9_.-]*)=(.*)$`)
	// Prefix-number suffix tokens such as worker-7 or node_12 -> worker-<NUM>.
	suffixNumRe = regexp.MustCompile(`^([A-Za-z][A-Za-z0-9]*[-_])([0-9]+)$`)
)

// Tokenize splits a raw log line into masked template tokens.
//
// Lines longer than maxLineBytes are truncated at a rune boundary and a
// <TRUNC> token is appended. Token lists longer than maxTokens are cut and
// likewise marked. Both bounds make very long lines cheap and deterministic.
// A whitespace-only line yields a nil slice.
func Tokenize(line string, maxLineBytes, maxTokens int) []string {
	if maxLineBytes <= 0 {
		maxLineBytes = DefaultMaxLineBytes
	}
	if maxTokens <= 0 {
		maxTokens = DefaultMaxTokens
	}

	truncatedLine := false
	if len(line) > maxLineBytes {
		line = truncateRunes(line, maxLineBytes)
		truncatedLine = true
	}

	raw := strings.Fields(line)
	truncatedTokens := false
	if len(raw) > maxTokens {
		raw = raw[:maxTokens]
		truncatedTokens = true
	}

	tokens := make([]string, 0, len(raw)+1)
	for _, t := range raw {
		tokens = append(tokens, MaskToken(t))
	}
	if truncatedLine || truncatedTokens {
		tokens = append(tokens, VarTruncated)
	}
	return tokens
}

// MaskToken converts a single whitespace-delimited token into its masked
// representation. The order matters: specific shapes (UUID, IP) are checked
// before generic ones (hex, number, key=value).
func MaskToken(t string) string {
	// Quoted strings ("..." or '...'), including unterminated ones produced
	// by naive whitespace splitting.
	if len(t) >= 2 && (t[0] == '"' || t[0] == '\'') {
		return VarStr
	}
	if uuidRe.MatchString(t) {
		return VarUUID
	}
	if ipRe.MatchString(t) {
		return VarIP
	}
	// Numbers must be checked before hex: 8+ decimal digits would otherwise
	// match the hex character class and be mislabeled <HEX>.
	if numRe.MatchString(t) {
		return VarNum
	}
	if hex0xRe.MatchString(t) || (hexRe.MatchString(t) && digitRe.MatchString(t)) {
		return VarHex
	}
	// URL-ish paths: mask all-digit path segments only, so "/api/v1/orders/9"
	// -> "/api/v1/orders/<NUM>" while the version literal "v1" survives.
	if strings.Contains(t, "/") {
		if masked := maskPathSegments(t); masked != t {
			return masked
		}
	}
	if m := kvRe.FindStringSubmatch(t); m != nil {
		// Keep the key literal so "status=200" and "latency=12ms" never merge.
		return m[1] + "=<VAL>"
	}
	if m := suffixNumRe.FindStringSubmatch(t); m != nil {
		return m[1] + VarNum
	}
	return t
}

// IsVar reports whether a masked token is a pure variable placeholder.
// key=<VAL> tokens are not variables: the literal key must be preserved.
func IsVar(t string) bool {
	switch t {
	case VarNum, VarUUID, VarHex, VarIP, VarStr:
		return true
	}
	return false
}

// maskPathSegments replaces every all-digit path segment with <NUM>.
func maskPathSegments(t string) string {
	segs := strings.Split(t, "/")
	changed := false
	for i, s := range segs {
		if s != "" && isAllDigits(s) {
			segs[i] = VarNum
			changed = true
		}
	}
	if !changed {
		return t
	}
	return strings.Join(segs, "/")
}

func isAllDigits(s string) bool {
	for _, r := range s {
		if r < '0' || r > '9' {
			return false
		}
	}
	return len(s) > 0
}

func truncateRunes(s string, maxBytes int) string {
	if len(s) <= maxBytes {
		return s
	}
	s = s[:maxBytes]
	for len(s) > 0 {
		r, size := utf8.DecodeLastRuneInString(s)
		if r == utf8.RuneError && size == 1 {
			s = s[:len(s)-1]
			continue
		}
		break
	}
	return s
}
