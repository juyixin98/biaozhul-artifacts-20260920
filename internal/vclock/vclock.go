// Package vclock implements vector clocks over string node IDs.
//
// A clock maps a node ID to its monotonically increasing event counter.
// Clocks are compared using the standard partial order: clock a
// happens-before clock b when every entry of a is less than or equal to
// the corresponding entry of b (and they are not equal). Entries absent
// from a map are treated as zero.
package vclock

// Clock is a vector clock: node ID -> counter.
type Clock map[string]int64

// Copy returns an independent copy of c.
func Copy(c Clock) Clock {
	out := make(Clock, len(c))
	for k, v := range c {
		out[k] = v
	}
	return out
}

// MergeInto folds every entry of src into dst, taking the maximum per node.
func MergeInto(dst, src Clock) {
	for k, v := range src {
		if v > dst[k] {
			dst[k] = v
		}
	}
}

// Merged returns a new clock holding the per-node maximum of a and b.
func Merged(a, b Clock) Clock {
	out := Copy(a)
	MergeInto(out, b)
	return out
}

// Le reports whether a <= b in the vector-clock partial order: every
// counter of a is no greater than the matching counter of b. Missing
// counters read as zero.
func Le(a, b Clock) bool {
	for k, v := range a {
		if v > b[k] {
			return false
		}
	}
	return true
}

// Equal reports whether the two clocks carry the same counters.
func Equal(a, b Clock) bool {
	return Le(a, b) && Le(b, a)
}

// Relation describes how two clocks are ordered.
type Relation int

const (
	// EqualRel means both clocks are identical.
	EqualRel Relation = iota
	// Before means a happens-before b (a is strictly older).
	Before
	// After means b happens-before a (a is strictly newer).
	After
	// Concurrent means neither clock dominates the other.
	Concurrent
)

func (r Relation) String() string {
	switch r {
	case EqualRel:
		return "equal"
	case Before:
		return "before"
	case After:
		return "after"
	default:
		return "concurrent"
	}
}

// Compare classifies the ordering of a relative to b.
func Compare(a, b Clock) Relation {
	aLeB := Le(a, b)
	bLeA := Le(b, a)
	switch {
	case aLeB && bLeA:
		return EqualRel
	case aLeB:
		return Before
	case bLeA:
		return After
	default:
		return Concurrent
	}
}
