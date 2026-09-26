// Package rangespec parses and resolves HTTP byte-range requests per
// RFC 9110 §14 (Range, Content-Range) and §13.1.3 (If-Range).
//
// Parsing is deliberately strict about the wire syntax but liberal in the
// one way the standard mandates: a syntactically invalid Range header is an
// error the caller treats as "no Range header", i.e. the server sends the
// full representation rather than a 4xx.
package rangespec

import (
	"errors"
	"fmt"
	"strconv"
	"strings"
)

// RawRange is one member of a bytes range set before resolution against a
// representation length. Exactly one of the forms applies:
//
//   - suffix:      Suffix == true,  SuffixLen = N          ("-N")
//   - open-ended:  OpenEnded == true, First = pos           ("pos-")
//   - closed:      all other fields, First..Last inclusive ("first-last")
type RawRange struct {
	First     int64
	Last      int64
	OpenEnded bool
	Suffix    bool
	SuffixLen int64
}

// String renders the range in Range-header wire form (without "bytes=").
func (r RawRange) String() string {
	switch {
	case r.Suffix:
		return fmt.Sprintf("-%d", r.SuffixLen)
	case r.OpenEnded:
		return fmt.Sprintf("%d-", r.First)
	default:
		return fmt.Sprintf("%d-%d", r.First, r.Last)
	}
}

// ErrInvalidRange is returned for a syntactically invalid Range header.
var ErrInvalidRange = errors.New("invalid or unsupported Range header")

// Parse parses a "bytes=..." Range header value. It returns the ordered list
// of raw ranges; duplicates and overlaps are preserved and left to the
// resolver/server policy. A non-bytes range unit or any malformed member
// yields ErrInvalidRange, in which case the caller MUST ignore the header.
func Parse(header string) ([]RawRange, error) {
	const prefix = "bytes="
	if len(header) < len(prefix) || !strings.EqualFold(header[:len(prefix)], prefix) {
		return nil, fmt.Errorf("%w: unknown range unit", ErrInvalidRange)
	}
	body := header[len(prefix):]
	members := strings.Split(body, ",")
	if len(members) == 1 && strings.TrimSpace(members[0]) == "" {
		return nil, fmt.Errorf("%w: empty range set", ErrInvalidRange)
	}

	out := make([]RawRange, 0, len(members))
	for _, member := range members {
		rr, err := parseMember(member)
		if err != nil {
			return nil, err
		}
		out = append(out, rr)
	}
	return out, nil
}

func parseMember(member string) (RawRange, error) {
	spec := strings.TrimSpace(member)
	if spec == "" {
		return RawRange{}, fmt.Errorf("%w: empty member", ErrInvalidRange)
	}
	// Interior whitespace is not allowed by the grammar.
	if strings.ContainsAny(spec, " \t") {
		return RawRange{}, fmt.Errorf("%w: interior whitespace in %q", ErrInvalidRange, spec)
	}
	dash := strings.IndexByte(spec, '-')
	if dash < 0 || strings.Count(spec, "-") != 1 {
		return RawRange{}, fmt.Errorf("%w: no single hyphen in %q", ErrInvalidRange, spec)
	}
	left, right := spec[:dash], spec[dash+1:]

	if left == "" {
		// Suffix form: -suffix-length.
		n, err := parseDigits(right)
		if err != nil {
			return RawRange{}, err
		}
		return RawRange{Suffix: true, SuffixLen: n}, nil
	}

	first, err := parseDigits(left)
	if err != nil {
		return RawRange{}, err
	}
	if right == "" {
		return RawRange{First: first, OpenEnded: true}, nil
	}
	last, err := parseDigits(right)
	if err != nil {
		return RawRange{}, err
	}
	if last < first {
		return RawRange{}, fmt.Errorf("%w: last-pos %d < first-pos %d", ErrInvalidRange, last, first)
	}
	return RawRange{First: first, Last: last}, nil
}

// parseDigits accepts only a non-empty run of ASCII digits (no sign, no
// spaces) that fits in int64.
func parseDigits(s string) (int64, error) {
	if s == "" {
		return 0, fmt.Errorf("%w: empty number", ErrInvalidRange)
	}
	n, err := strconv.ParseInt(s, 10, 64)
	if err != nil {
		return 0, fmt.Errorf("%w: number %q: %v", ErrInvalidRange, s, err)
	}
	return n, nil
}
