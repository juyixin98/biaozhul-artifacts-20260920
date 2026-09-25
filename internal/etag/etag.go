// Package etag parses and compares HTTP entity tags (RFC 9110),
// enforcing that weak tags never satisfy a strong comparison.
package etag

import (
	"fmt"
	"strings"
)

// ETag is a parsed entity tag.
type ETag struct {
	// Value is the opaque tag without quotes and without the W/ prefix.
	Value string
	// Weak reports whether the tag carried the W/ weakness indicator.
	Weak bool
}

// Parse parses a single entity tag such as `"abc"` or `W/"abc"`.
func Parse(s string) (ETag, error) {
	s = strings.TrimSpace(s)
	var e ETag
	if strings.HasPrefix(s, "W/") {
		e.Weak = true
		s = strings.TrimPrefix(s, "W/")
	}
	if len(s) < 2 || !strings.HasPrefix(s, `"`) || !strings.HasSuffix(s, `"`) {
		return ETag{}, fmt.Errorf("malformed entity tag %q", s)
	}
	e.Value = s[1 : len(s)-1]
	if e.Value == "" {
		return ETag{}, fmt.Errorf("empty entity tag")
	}
	return e, nil
}

// String renders the tag for an ETag header, re-adding the W/ prefix
// when the tag is weak.
func (e ETag) String() string {
	if e.Weak {
		return `W/"` + e.Value + `"`
	}
	return `"` + e.Value + `"`
}

// StrongMatch reports whether a and b match under the strong comparison
// function: both must be strong (non-weak) and byte-identical.
func StrongMatch(a, b ETag) bool {
	return !a.Weak && !b.Weak && a.Value == b.Value
}

// Condition is a parsed If-Match / If-None-Match header value.
type Condition struct {
	// Star is true when the header was exactly `*`.
	Star bool
	// Tags holds the parsed entity tags when Star is false.
	Tags []ETag
}

// ParseCondition parses an If-Match or If-None-Match header value.
func ParseCondition(header string) (Condition, error) {
	header = strings.TrimSpace(header)
	if header == "" {
		return Condition{}, fmt.Errorf("empty condition")
	}
	if header == "*" {
		return Condition{Star: true}, nil
	}
	var c Condition
	for _, part := range strings.Split(header, ",") {
		e, err := Parse(part)
		if err != nil {
			return Condition{}, err
		}
		c.Tags = append(c.Tags, e)
	}
	return c, nil
}

// StronglyMatchesAny reports whether current matches any tag in the
// condition under strong comparison. A `*` condition matches only when
// the resource exists (currentOK is true).
func (c Condition) StronglyMatchesAny(current ETag, currentOK bool) bool {
	if c.Star {
		return currentOK
	}
	if !currentOK {
		return false
	}
	for _, t := range c.Tags {
		if StrongMatch(t, current) {
			return true
		}
	}
	return false
}

// WeaklyMatchesAny reports whether current matches any tag in the
// condition under weak comparison (used by If-None-Match): identical
// opaque values match regardless of weakness. A `*` condition matches
// whenever the resource exists.
func (c Condition) WeaklyMatchesAny(current ETag, currentOK bool) bool {
	if c.Star {
		return currentOK
	}
	if !currentOK {
		return false
	}
	for _, t := range c.Tags {
		if t.Value == current.Value {
			return true
		}
	}
	return false
}
