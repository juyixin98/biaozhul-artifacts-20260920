// Package clock implements vector clocks and the happens-before partial order.
//
// A vector clock maps a node id to its event counter. The comparison between
// two clocks is a partial order: two clocks are either equal, strictly ordered
// (one happens-before the other), or concurrent (incomparable).
package clock

// VectorClock is a node -> counter map. Zero values represent the initial clock.
type VectorClock map[string]int

// Copy returns an independent copy of vc.
func Copy(vc VectorClock) VectorClock {
	out := make(VectorClock, len(vc))
	for k, v := range vc {
		out[k] = v
	}
	return out
}

// Tick returns a copy of vc with node's counter incremented.
// It models a local event at node.
func Tick(vc VectorClock, node string) VectorClock {
	out := Copy(vc)
	out[node]++
	return out
}

// MergeInto mutates dst, taking the component-wise maximum with src.
// It models learning the causal history carried by src.
func MergeInto(dst, src VectorClock) {
	for k, v := range src {
		if v > dst[k] {
			dst[k] = v
		}
	}
}

// Merge returns the component-wise maximum of a and b.
func Merge(a, b VectorClock) VectorClock {
	out := Copy(a)
	MergeInto(out, b)
	return out
}

// Descends reports whether ancestor happens-before-or-equal descendant:
// every component of ancestor is <= the corresponding component of descendant.
func Descends(descendant, ancestor VectorClock) bool {
	for k, v := range ancestor {
		if descendant[k] < v {
			return false
		}
	}
	return true
}

// Equal reports component-wise equality.
func Equal(a, b VectorClock) bool {
	return Descends(a, b) && Descends(b, a)
}

// Relation is the outcome of comparing two vector clocks.
type Relation int

const (
	// RelBefore means a happens-before b (a is strictly older than b).
	RelBefore Relation = iota - 1
	// RelAfter means b happens-before a (a is strictly newer than b).
	RelAfter
	// RelEqual means the clocks are identical.
	RelEqual
	// RelConcurrent means the clocks are incomparable.
	RelConcurrent
)

// Compare returns how clock a relates to clock b.
func Compare(a, b VectorClock) Relation {
	aLEb := Descends(b, a) // a <= b
	bLEa := Descends(a, b) // b <= a
	switch {
	case aLEb && bLEa:
		return RelEqual
	case aLEb:
		return RelBefore
	case bLEa:
		return RelAfter
	default:
		return RelConcurrent
	}
}
