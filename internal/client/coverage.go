package client

import (
	"fmt"

	"github.com/example/rangeserver/internal/rangespec"
)

// coverage tracks which byte offsets of the expected representation have
// been delivered. It enforces two invariants during reassembly:
//
//  1. a segment may not extend past the announced representation size;
//  2. bytes delivered twice (overlapping ranges) must be identical —
//     otherwise the server sent contradictory content for one offset.
type coverage struct {
	delivered []bool
	size      int64
	got       int64
}

func newCoverage(size int64) *coverage {
	return &coverage{delivered: make([]bool, size), size: size}
}

// add places data at its absolute offsets, returning whether any of those
// offsets were already covered. Contradictory overlapping bytes or an
// out-of-bounds segment are errors.
func (c *coverage) add(r rangespec.Resolved, data, assembly []byte) (bool, error) {
	if r.First < 0 || r.Last >= c.size || r.First > r.Last {
		return false, fmt.Errorf("segment %d-%d outside representation of size %d", r.First, r.Last, c.size)
	}
	if int64(len(data)) != r.Length() {
		return false, fmt.Errorf("segment %d-%d delivered %d bytes", r.First, r.Last, len(data))
	}
	overlap := false
	for i, b := range data {
		off := r.First + int64(i)
		if c.delivered[off] {
			if assembly[off] != b {
				return false, fmt.Errorf("conflicting byte at offset %d: segments disagree", off)
			}
			overlap = true
			continue
		}
		c.delivered[off] = true
		c.got++
		assembly[off] = b
	}
	return overlap, nil
}

func (c *coverage) complete() bool { return c.got == c.size }

func (c *coverage) deliveredCount() int64 { return c.got }
