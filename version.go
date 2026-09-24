package main

import "sort"

// Version is one stored value of a key together with the vector clock of the
// write that produced it.
//
// The Id is a globally unique, opaque identifier of the write (the reference
// implementation creates "<replica>-<counter>" ids). Id equality is what makes
// message delivery idempotent: re-delivering the exact same message (at-least-
// once transport, a retry, a sync loop) must not duplicate the version.
type Version struct {
	ID     string `json:"id"`
	Origin string `json:"origin"`
	Clock  Clock  `json:"clock"`
	Value  string `json:"value"`
}

// Clone returns a deep copy of the version.
func (v Version) Clone() Version {
	return Version{ID: v.ID, Origin: v.Origin, Clock: v.Clock.Clone(), Value: v.Value}
}

// IncomingOutcome records what happened to one delivered/synced version.
type IncomingOutcome string

const (
	// OutcomeAccepted: the version was newly stored.
	OutcomeAccepted IncomingOutcome = "accepted"
	// OutcomeDuplicate: a version with the same id was already present
	// (idempotent re-delivery; nothing changed).
	OutcomeDuplicate IncomingOutcome = "duplicate"
	// OutcomeSuperseded: the version was causally older than (or equal in
	// history to) a version already known and was discarded. A stale write
	// arriving late lands here.
	OutcomeSuperseded IncomingOutcome = "superseded"
)

// MergeVersions folds incoming versions into the versions already stored for a
// key and returns the resulting antichain plus one outcome per incoming id.
//
// The result is the maximal antichain under the vector-clock partial order:
//   - a version that happened-before another is dropped (causal overwrite);
//   - causally concurrent versions are all retained as siblings;
//   - equal clocks (e.g. duplicate id re-sent under a different envelope) are
//     treated as already known and the copy already stored wins.
//
// The operation is associative, commutative and idempotent over sets of
// versions, which is exactly the property synchronization needs to converge
// regardless of message ordering or retransmission.
func MergeVersions(existing []Version, incoming []Version) (merged []Version, outcomes map[string]IncomingOutcome) {
	outcomes = make(map[string]IncomingOutcome, len(incoming))

	// alive holds the antichain being built.
	alive := make([]Version, 0, len(existing)+len(incoming))
	knownIDs := make(map[string]struct{}, len(existing)+len(incoming))
	for _, v := range existing {
		alive = append(alive, v)
		knownIDs[v.ID] = struct{}{}
	}

	for _, in := range incoming {
		if _, ok := knownIDs[in.ID]; ok {
			// Exact re-delivery of the same write. Idempotent no-op.
			outcomes[in.ID] = OutcomeDuplicate
			continue
		}

		// Compare the newcomer against every version currently alive.
		dominatedBy := -1 // index of an alive version that dominates `in`
		killed := []int{} // alive versions `in` dominates
		for i, cur := range alive {
			switch CompareClocks(in.Clock, cur.Clock) {
			case ClockAfter: // in is strictly newer: it replaces cur
				killed = append(killed, i)
			case ClockBefore, ClockEqual: // cur is newer or same history
				dominatedBy = i
			case ClockConcurrent:
				// keep both as siblings
			}
		}

		if dominatedBy >= 0 {
			outcomes[in.ID] = OutcomeSuperseded
			continue
		}
		outcomes[in.ID] = OutcomeAccepted
		knownIDs[in.ID] = struct{}{}

		// Remove the alive entries the newcomer dominated. Iterate in reverse
		// so the indices collected above stay valid.
		for j := len(killed) - 1; j >= 0; j-- {
			alive = append(alive[:killed[j]], alive[killed[j]+1:]...)
		}
		alive = append(alive, in)
	}

	sort.Slice(alive, func(i, j int) bool { return alive[i].ID < alive[j].ID })
	return alive, outcomes
}
