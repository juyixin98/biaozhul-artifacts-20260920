package rangespec

import (
	"errors"
	"fmt"
	"sort"
	"strconv"
	"strings"
)

// ErrTooManyRanges is returned when a request asks for more ranges than the
// server policy allows.
var ErrTooManyRanges = errors.New("rangespec: too many ranges")

// Resolved is a satisfiable, already-clamped byte interval.
type Resolved struct {
	First int64 // inclusive
	Last  int64 // inclusive
}

// Length returns the number of bytes in the interval.
func (r Resolved) Length() int64 { return r.Last - r.First + 1 }

// UnsatisfiableError carries the representation length needed to render
// "Content-Range: bytes */size" on a 416 response.
type UnsatisfiableError struct {
	RepresentationLength int64
}

func (e *UnsatisfiableError) Error() string {
	return fmt.Sprintf("rangespec: no satisfiable range in representation of length %d", e.RepresentationLength)
}

// Resolve maps one syntactic Spec onto a representation of size bytes.
// The second result is false when the range cannot be satisfied.
func Resolve(s Spec, size int64) (Resolved, bool, error) {
	if size < 0 {
		return Resolved{}, false, fmt.Errorf("rangespec: negative representation length %d", size)
	}
	switch s.Kind {
	case Closed:
		if size == 0 || s.First > size-1 {
			return Resolved{}, false, nil
		}
		last := s.Last
		if last > size-1 {
			last = size - 1 // clamp an overlong range to the representation
		}
		return Resolved{First: s.First, Last: last}, true, nil
	case OpenEnded:
		if size == 0 || s.First > size-1 {
			return Resolved{}, false, nil
		}
		return Resolved{First: s.First, Last: size - 1}, true, nil
	case Suffix:
		if size == 0 {
			return Resolved{}, false, nil
		}
		n := s.SuffixLength
		if n >= size {
			// Asking for more than the whole thing yields the whole thing.
			return Resolved{First: 0, Last: size - 1}, true, nil
		}
		return Resolved{First: size - n, Last: size - 1}, true, nil
	default:
		return Resolved{}, false, fmt.Errorf("rangespec: unknown range kind %d", s.Kind)
	}
}

// CheckCount enforces the server-side bound on the number of requested
// ranges. maxRanges must be positive.
func CheckCount(specs []Spec, maxRanges int) error {
	if maxRanges <= 0 {
		return fmt.Errorf("rangespec: invalid range limit %d", maxRanges)
	}
	if len(specs) > maxRanges {
		return fmt.Errorf("%w: %d requested, limit %d", ErrTooManyRanges, len(specs), maxRanges)
	}
	return nil
}

// ResolveAll resolves and clamps every spec against size, dropping
// unsatisfiable ones. If none are satisfiable it returns
// *UnsatisfiableError. The returned ranges are sorted and coalesced:
// overlapping intervals are merged (which also covers duplicate ranges).
// Merely adjacent intervals are kept separate so a tiled request produces
// multiple multipart parts; both forms are valid multipart/byteranges.
func ResolveAll(specs []Spec, size int64) ([]Resolved, error) {
	resolved := make([]Resolved, 0, len(specs))
	for _, s := range specs {
		r, ok, err := Resolve(s, size)
		if err != nil {
			return nil, err
		}
		if ok {
			resolved = append(resolved, r)
		}
	}
	if len(resolved) == 0 {
		return nil, &UnsatisfiableError{RepresentationLength: size}
	}
	return Coalesce(resolved), nil
}

// Coalesce sorts intervals and merges overlapping or adjacent ones.
func Coalesce(rs []Resolved) []Resolved {
	if len(rs) <= 1 {
		return rs
	}
	sorted := make([]Resolved, len(rs))
	copy(sorted, rs)
	sort.Slice(sorted, func(i, j int) bool {
		if sorted[i].First != sorted[j].First {
			return sorted[i].First < sorted[j].First
		}
		return sorted[i].Last < sorted[j].Last
	})

	merged := []Resolved{sorted[0]}
	for _, r := range sorted[1:] {
		last := &merged[len(merged)-1]
		if r.First <= last.Last {
			// Overlap (or duplicate): extend the current interval.
			if r.Last > last.Last {
				last.Last = r.Last
			}
			continue
		}
		merged = append(merged, r)
	}
	return merged
}

// ContentRange renders a Content-Range header value for a satisfied range.
func ContentRange(r Resolved, size int64) string {
	return fmt.Sprintf("bytes %d-%d/%d", r.First, r.Last, size)
}

// UnsatisfiableContentRange renders "bytes */size" for a 416 response.
func UnsatisfiableContentRange(size int64) string {
	return "bytes */" + strconv.FormatInt(size, 10)
}

// Part pairs a resolved interval with the selected-representation bytes it
// covers, so multipart rendering stays allocation-light.
type Part struct {
	Range       Resolved
	ContentType string
}

// MultipartAssembler builds multipart/byteranges bodies against one
// representation. It is constructed once per response and never mutated
// after Bytes is called.
type MultipartAssembler struct {
	boundary string
	rep      []byte
	size     int64
	parts    []Part
}

// NewMultipartAssembler returns an assembler using boundary. The rep slice
// is treated as immutable.
func NewMultipartAssembler(boundary string, rep []byte, parts []Part) *MultipartAssembler {
	return &MultipartAssembler{
		boundary: boundary,
		rep:      rep,
		size:     int64(len(rep)),
		parts:    parts,
	}
}

// ContentType returns the multipart media type including the boundary.
func (m *MultipartAssembler) ContentType() string {
	return "multipart/byteranges; boundary=" + m.boundary
}

// Body assembles the multipart body. Bytes are sliced (not copied) out of
// the representation; callers must not mutate the underlying representation
// before the body has been written.
func (m *MultipartAssembler) Body() []byte {
	var b strings.Builder
	// Boundary strings and headers are small; preallocate a rough estimate.
	b.Grow(int(m.size) + len(m.parts)*96)
	for _, p := range m.parts {
		b.WriteString("--")
		b.WriteString(m.boundary)
		b.WriteString("\r\n")
		if p.ContentType != "" {
			b.WriteString("Content-Type: ")
			b.WriteString(p.ContentType)
			b.WriteString("\r\n")
		}
		b.WriteString("Content-Range: ")
		b.WriteString(ContentRange(p.Range, m.size))
		b.WriteString("\r\n\r\n")
		b.Write(m.rep[p.Range.First : p.Range.Last+1])
		b.WriteString("\r\n")
	}
	b.WriteString("--")
	b.WriteString(m.boundary)
	b.WriteString("--\r\n")
	return []byte(b.String())
}
