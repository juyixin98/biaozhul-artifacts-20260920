// Package pattern implements the local wildcard rules used both for excluded
// applications (policy) and for classification rules.
//
// Syntax is the Go path.Match subset, intentionally local-only (no regex):
//
//	'?' matches any single character
//	'*' matches any (possibly empty) run of characters
//	everything else matches itself
//
// Matching is case-insensitive (app names arrive from different OSes with
// different casing).
package pattern

import (
	"errors"
	"strings"
)

// ErrBadPattern is returned for malformed patterns (trailing escape).
var ErrBadPattern = errors.New("malformed wildcard pattern")

// Match reports whether the lower-cased name matches the lower-cased pattern.
func Match(pattern, name string) bool {
	ok, err := match(strings.ToLower(pattern), strings.ToLower(name))
	return ok && err == nil
}

// match is the standard recursive glob matcher.
func match(p, s string) (bool, error) {
	for len(p) > 0 {
		switch p[0] {
		case '*':
			// Collapse consecutive stars.
			for len(p) > 0 && p[0] == '*' {
				p = p[1:]
			}
			if len(p) == 0 {
				return true, nil
			}
			for i := 0; i <= len(s); i++ {
				ok, err := match(p, s[i:])
				if err != nil {
					return false, err
				}
				if ok {
					return true, nil
				}
			}
			return false, nil
		case '?':
			if len(s) == 0 {
				return false, nil
			}
			s = s[1:]
		case '[':
			// '[' is treated as a literal; the documented subset is * and ?.
			if len(s) > 0 && s[0] == '[' {
				s = s[1:]
			}
		case '\\':
			// Backslash escapes the next byte; a trailing backslash is invalid.
			if len(p) < 2 {
				return false, ErrBadPattern
			}
			if len(s) == 0 || s[0] != p[1] {
				return false, nil
			}
			s = s[1:]
			p = p[1:]
		default:
			if len(s) == 0 || s[0] != p[0] {
				return false, nil
			}
			s = s[1:]
		}
		p = p[1:]
	}
	return len(s) == 0, nil
}

// Any reports whether name matches at least one of the patterns.
func Any(patterns []string, name string) bool {
	for _, p := range patterns {
		if Match(p, name) {
			return true
		}
	}
	return false
}

// Validate returns an error for patterns the matcher cannot parse.
func Validate(p string) error {
	for i := 0; i < len(p); i++ {
		if p[i] == '\\' {
			if i+1 >= len(p) {
				return ErrBadPattern
			}
			i++
		}
	}
	return nil
}
