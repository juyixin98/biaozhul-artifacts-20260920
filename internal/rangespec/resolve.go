package rangespec

import (
	"errors"
	"fmt"
)

// Resolved is a byte range clipped to a representation: inclusive [Start,End].
type Resolved struct {
	Start int64
	End   int64
}

// Length returns the number of bytes in the range.
func (r Resolved) Length() int64 { return r.End - r.Start + 1 }

// ContentRange renders a Content-Range header value for this range.
func (r Resolved) ContentRange(total int64) string {
	return fmt.Sprintf("bytes %d-%d/%d", r.Start, r.End, total)
}

// ErrUnsatisfiable means no member of a syntactically valid range set
// intersects the representation. Callers must answer 416 and carry the
// representation length in Content-Range: bytes */<size>.
var ErrUnsatisfiable = errors.New("byte range not satisfiable")

// Resolve clips a parsed range set to a representation of size bytes.
//
// Semantics per RFC 9110 §14.1.1:
//   - closed "first-last": last is clamped to size-1; first >= size drops.
//   - open "first-":      covers first..size-1; first >= size drops.
//   - suffix "-N":        last N bytes; N >= size means the whole
//     representation; N == 0 produces no bytes and is dropped.
//
// A set with at least one member that still intersects is satisfiable:
// dropped members are ignored, duplicates and overlaps preserved. If every
// member drops out (which includes size == 0), ErrUnsatisfiable wraps the
// size for the 416 response.
func Resolve(raw []RawRange, size int64) ([]Resolved, error) {
	out := make([]Resolved, 0, len(raw))
	for _, rr := range raw {
		rs, ok := resolveOne(rr, size)
		if ok {
			out = append(out, rs)
		}
	}
	if len(out) == 0 {
		return nil, fmt.Errorf("%w: representation length %d", ErrUnsatisfiable, size)
	}
	return out, nil
}

// resolveOne returns ok=false for a member that does not intersect.
func resolveOne(rr RawRange, size int64) (Resolved, bool) {
	if size <= 0 {
		return Resolved{}, false
	}
	switch {
	case rr.Suffix:
		if rr.SuffixLen <= 0 {
			return Resolved{}, false
		}
		start := size - rr.SuffixLen
		if start < 0 {
			start = 0
		}
		return Resolved{Start: start, End: size - 1}, start <= size-1
	case rr.OpenEnded:
		if rr.First >= size {
			return Resolved{}, false
		}
		return Resolved{Start: rr.First, End: size - 1}, true
	default:
		if rr.First >= size {
			return Resolved{}, false
		}
		end := rr.Last
		if end >= size {
			end = size - 1
		}
		return Resolved{Start: rr.First, End: end}, true
	}
}

// UnsatisfiableContentRange renders the Content-Range header used on a 416:
// "bytes */<size>".
func UnsatisfiableContentRange(size int64) string {
	return fmt.Sprintf("bytes */%d", size)
}
