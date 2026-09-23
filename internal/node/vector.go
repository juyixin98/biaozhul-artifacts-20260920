// Package node implements the per-node causal-broadcast state machine:
// a vector clock, a delivery buffer for messages whose causal predecessors
// have not arrived yet, and duplicate suppression.
package node

// VC is a vector clock indexed by the global node index.
type VC []int

// Clone returns an independent copy.
func (v VC) Clone() VC {
	out := make(VC, len(v))
	copy(out, v)
	return out
}

// Merge raises every component to max(v[i], other[i]).
func (v VC) Merge(other VC) {
	for i := range other {
		if other[i] > v[i] {
			v[i] = other[i]
		}
	}
}

// Deliverable reports whether a message with clock msgVC is causally
// deliverable at a node whose current clock is cur:
//
//	msgVC[sender] == cur[sender]+1
//	msgVC[i]    <= cur[i]   for every i != sender
//
// This is the classic Birman-style causal delivery test expressed on
// vector clocks.
func Deliverable(cur VC, msgVC VC, sender int) bool {
	if len(msgVC) != len(cur) {
		return false
	}
	if msgVC[sender] != cur[sender]+1 {
		return false
	}
	for i := range msgVC {
		if i == sender {
			continue
		}
		if msgVC[i] > cur[i] {
			return false
		}
	}
	return true
}

// Missing describes one unsatisfied predecessor requirement of a buffered
// message: node From is only known up to Have, but the message requires it
// to be at least Need.
type Missing struct {
	From int
	Have int
	Need int
}

// MissingDeps lists every component that still blocks delivery of msgVC.
// An empty result means the message is deliverable now. The sender
// component reports the earliest missing sequence (cur[sender]+1) rather
// than the message's own sequence, so a same-stream gap (A3 arriving
// without A2) points at A2 as the first thing missing.
func MissingDeps(cur VC, msgVC VC, sender int) []Missing {
	var out []Missing
	if msgVC[sender] > cur[sender]+1 {
		out = append(out, Missing{From: sender, Have: cur[sender], Need: cur[sender] + 1})
	}
	for i := range msgVC {
		if i == sender {
			continue
		}
		if msgVC[i] > cur[i] {
			out = append(out, Missing{From: i, Have: cur[i], Need: msgVC[i]})
		}
	}
	return out
}
