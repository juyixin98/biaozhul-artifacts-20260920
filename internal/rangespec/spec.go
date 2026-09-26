// Package rangespec implements RFC 9110 byte-range semantics: parsing of the
// Range header, resolution against a representation length, Content-Range
// formatting, multipart/byteranges assembly, the If-Range precondition, and
// Accept-Encoding representation selection.
package rangespec

import (
	"errors"
	"fmt"
	"strconv"
	"strings"
)

// ErrMalformedRange means a Range header could not be parsed. A server
// receiving such a header ignores it and sends the full representation.
var ErrMalformedRange = errors.New("rangespec: malformed byte range")

// Kind discriminates the three byte-range-spec forms.
type Kind int

const (
	// Closed is "first-last": a closed inclusive interval.
	Closed Kind = iota
	// OpenEnded is "first-": from first to the end of the representation.
	OpenEnded
	// Suffix is "-suffix-length": the final suffix-length bytes.
	Suffix
)

// Spec is one byte-range-spec as written by the client.
type Spec struct {
	Kind         Kind
	First        int64 // Closed / OpenEnded
	Last         int64 // Closed only; inclusive
	SuffixLength int64 // Suffix only
}

func (s Spec) String() string {
	switch s.Kind {
	case Closed:
		return fmt.Sprintf("bytes=%d-%d", s.First, s.Last)
	case OpenEnded:
		return fmt.Sprintf("bytes=%d-", s.First)
	case Suffix:
		return fmt.Sprintf("bytes=-%d", s.SuffixLength)
	default:
		return "bytes=?"
	}
}

// RangeUnitBytes is the only range unit this server supports.
const RangeUnitBytes = "bytes"

// ParseHeader parses a "bytes=..." Range header value into specs in the
// order the client sent them. It performs syntactic validation only;
// satisfiability is decided by Resolve against a concrete length.
func ParseHeader(header string) ([]Spec, error) {
	h := strings.TrimSpace(header)
	eq := strings.IndexByte(h, '=')
	if eq < 0 {
		return nil, fmt.Errorf("%w: missing '='", ErrMalformedRange)
	}
	unit := strings.TrimSpace(h[:eq])
	if !strings.EqualFold(unit, RangeUnitBytes) {
		return nil, fmt.Errorf("%w: unsupported range unit %q", ErrMalformedRange, unit)
	}
	rawSpecs := strings.Split(h[eq+1:], ",")
	if len(rawSpecs) == 1 && strings.TrimSpace(rawSpecs[0]) == "" {
		return nil, fmt.Errorf("%w: empty range set", ErrMalformedRange)
	}

	specs := make([]Spec, 0, len(rawSpecs))
	for _, raw := range rawSpecs {
		s, err := parseOne(strings.TrimSpace(raw))
		if err != nil {
			return nil, err
		}
		specs = append(specs, s)
	}
	return specs, nil
}

func parseOne(s string) (Spec, error) {
	if s == "" {
		return Spec{}, fmt.Errorf("%w: empty range-spec", ErrMalformedRange)
	}
	dash := strings.IndexByte(s, '-')
	if dash < 0 {
		return Spec{}, fmt.Errorf("%w: %q has no '-'", ErrMalformedRange, s)
	}
	before, after := s[:dash], s[dash+1:]
	if strings.Contains(after, "-") {
		return Spec{}, fmt.Errorf("%w: %q has extra '-'", ErrMalformedRange, s)
	}

	if before == "" {
		// Suffix range: -suffix-length.
		n, err := parseNonNegative(after)
		if err != nil {
			return Spec{}, err
		}
		// RFC: a suffix-length of zero is syntactically valid but never
		// carries meaning; reject it rather than silently returning bytes.
		if n == 0 {
			return Spec{}, fmt.Errorf("%w: zero suffix-length", ErrMalformedRange)
		}
		return Spec{Kind: Suffix, SuffixLength: n}, nil
	}

	first, err := parseNonNegative(before)
	if err != nil {
		return Spec{}, err
	}
	if after == "" {
		return Spec{Kind: OpenEnded, First: first}, nil
	}
	last, err := parseNonNegative(after)
	if err != nil {
		return Spec{}, err
	}
	if first > last {
		return Spec{}, fmt.Errorf("%w: first-byte-pos %d > last-byte-pos %d", ErrMalformedRange, first, last)
	}
	return Spec{Kind: Closed, First: first, Last: last}, nil
}

func parseNonNegative(s string) (int64, error) {
	if s == "" || strings.TrimLeft(s, "0123456789") != "" {
		return 0, fmt.Errorf("%w: %q is not a non-negative integer", ErrMalformedRange, s)
	}
	n, err := strconv.ParseInt(s, 10, 64)
	if err != nil {
		return 0, fmt.Errorf("%w: %q: %v", ErrMalformedRange, s, err)
	}
	return n, nil
}
