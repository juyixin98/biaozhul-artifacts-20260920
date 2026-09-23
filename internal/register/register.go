// Package register implements a versioned, multi-replica register whose
// conflicts are tracked with vector clocks.
//
// A register keeps ALL concurrent (sibling) versions instead of silently
// choosing a winner. A client merge write must carry the COMPLETE context
// it overwrites: the context has to name every sibling currently alive
// for the key. Two rejection codes protect the invariant:
//
//   - unknown_context: a named version exists nowhere on this node
//   - stale_context:   a surviving sibling was left out of the context
//
// Either rejection leaves the store untouched, so a write can never
// silently discard a concurrent version the client did not merge.
//
// Versions arriving over the simulated network are merged symmetrically
// (union + prune dominated). That merge is order independent and
// idempotent, which is what makes duplicated and reordered deliveries
// harmless.
package register

import (
	"encoding/json"
	"fmt"
	"sort"

	"vcconflict/internal/vclock"
)

// Version is one surviving value of a register.
type Version struct {
	// ID is the globally unique version identifier ("node#counter").
	ID string `json:"id"`
	// Value is the raw client payload (arbitrary JSON, verbatim).
	Value json.RawMessage `json:"value"`
	// Clock is the version's vector clock.
	Clock vclock.Clock `json:"clock"`
	// Author is the node that accepted the write.
	Author string `json:"author"`
}

// Snapshot is an immutable, wire-serializable copy of a node's whole store.
type Snapshot struct {
	// Versions maps each key to its surviving (maximal) sibling versions.
	Versions map[string][]Version `json:"versions"`
}

// RejectError describes a write refused because its context was stale.
type RejectError struct {
	// Missing is the version IDs from the context that the store never saw.
	Missing []string
	// StaleUnseen is the unacknowledged sibling that the new clock would
	// dominate — the concrete version that would have been silently lost.
	StaleUnseen *Version
	// Reason is a short machine-readable code.
	Reason string
}

func (e *RejectError) Error() string {
	switch e.Reason {
	case "unknown_context":
		return fmt.Sprintf("write rejected: context references unknown version(s) %v", e.Missing)
	case "stale_context":
		v := e.StaleUnseen
		return fmt.Sprintf("write rejected: stale context; would overwrite unseen version %s (clock %v) by %s", v.ID, v.Clock, v.Author)
	default:
		return "write rejected: " + e.Reason
	}
}

// WriteReport summarizes an accepted write.
type WriteReport struct {
	Key string `json:"key"`
	// NewVersion is the descendant minted by the write.
	NewVersion Version `json:"new_version"`
	// Covered are exactly the context sibling IDs the new version replaces.
	Covered []string `json:"covered"`
}

// MergeReport summarizes one incoming replica snapshot merge.
type MergeReport struct {
	// Added are versions newly accepted.
	Added []string
	// Duplicate are versions already present (replayed/reordered delivery).
	Duplicate []string
	// Pruned are locally dominated versions removed by the merge.
	Pruned []string
	// PrunedIncoming are incoming dominated versions discarded.
	PrunedIncoming []string
}

// Store is a node-local, per-key set of versioned registers.
type Store struct {
	node string
	data map[string][]Version // maximal siblings per key
}

// NewStore creates an empty store owned by node.
func NewStore(node string) *Store {
	return &Store{node: node, data: make(map[string][]Version)}
}

// Node returns the owning node ID.
func (s *Store) Node() string { return s.node }

// Keys returns the sorted key names currently held.
func (s *Store) Keys() []string {
	out := make([]string, 0, len(s.data))
	for k := range s.data {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// Read returns the surviving siblings for key (nil when the key is absent).
func (s *Store) Read(key string) []Version {
	return s.data[key]
}

// highCounter returns the highest counter this node has ever used, derived
// deterministically from every surviving clock in the store.
func (s *Store) highCounter() int64 {
	var hi int64
	for _, versions := range s.data {
		for _, v := range versions {
			if c := v.Clock[s.node]; c > hi {
				hi = c
			}
		}
	}
	return hi
}

// Write applies a client merge write.
//
// ctxIDs is the FULL context the client observed: it must name every
// surviving sibling of the key on this node. The rule enforces the
// requirement that "a merging write carries the complete context it
// overwrites":
//
//   - context naming versions this node never saw        -> unknown_context
//   - context that omits any currently surviving sibling -> stale_context
//   - empty context on an absent key                     -> creates the key
//
// On success the new version's clock is the join of (a) every context
// clock and (b) every surviving sibling clock known to this node — a
// replica's local vector clock advances with gossip, so even an
// empty-context offline write inherits the causal history the node has
// already observed — ticked on this node's own component. The named
// siblings are replaced by that single descendant.
func (s *Store) Write(key string, value json.RawMessage, ctxIDs []string) (*WriteReport, error) {
	if len(value) > 0 {
		var probe any
		if err := json.Unmarshal(value, &probe); err != nil {
			return nil, fmt.Errorf("invalid JSON value: %w", err)
		}
	}

	existing := s.data[key]
	byID := make(map[string]Version, len(existing))
	for _, v := range existing {
		byID[v.ID] = v
	}

	// Deduplicate the declared context.
	seen := make(map[string]bool, len(ctxIDs))
	ctx := make([]string, 0, len(ctxIDs))
	for _, id := range ctxIDs {
		if !seen[id] {
			seen[id] = true
			ctx = append(ctx, id)
		}
	}

	// Every named context version must be known locally.
	var missing []string
	join := vclock.Clock{}
	for _, id := range ctx {
		v, ok := byID[id]
		if !ok {
			missing = append(missing, id)
			continue
		}
		vclock.MergeInto(join, v.Clock)
	}
	if len(missing) > 0 {
		sort.Strings(missing)
		return nil, &RejectError{Reason: "unknown_context", Missing: missing}
	}

	// The context must cover EVERY surviving sibling. An omitted sibling
	// would be overwritten without the client having merged it — refuse.
	if len(seen) != len(existing) {
		for _, v := range existing {
			if !seen[v.ID] {
				cand := v
				return nil, &RejectError{Reason: "stale_context", StaleUnseen: &cand}
			}
		}
	}

	// Fold in every surviving sibling: the node's local clock reflects
	// all causal history observed via gossip, not only the client context.
	known := vclock.Copy(join)
	for _, v := range existing {
		vclock.MergeInto(known, v.Clock)
	}

	counter := s.highCounter() + 1
	if c := known[s.node] + 1; c > counter {
		counter = c
	}
	newClock := vclock.Copy(known)
	newClock[s.node] = counter
	newID := fmt.Sprintf("%s#%d", s.node, counter)
	newVer := Version{
		ID:     newID,
		Value:  append(json.RawMessage(nil), value...),
		Clock:  newClock,
		Author: s.node,
	}

	report := &WriteReport{Key: key, NewVersion: newVer, Covered: append([]string{}, ctx...)}
	sort.Strings(report.Covered)
	s.data[key] = []Version{newVer}
	return report, nil
}

// MergeSnapshot folds a remote replica snapshot into this store. The
// operation is symmetric, commutative and idempotent: for each key the
// union of both version sets is taken and every dominated entry is
// pruned. Replaying the same snapshot changes nothing.
func (s *Store) MergeSnapshot(in Snapshot) map[string]*MergeReport {
	reports := make(map[string]*MergeReport, len(in.Versions))
	keys := make(map[string]struct{}, len(in.Versions))
	for k := range in.Versions {
		keys[k] = struct{}{}
	}
	for k := range s.data {
		keys[k] = struct{}{}
	}
	for key := range keys {
		reports[key] = s.mergeKey(key, in.Versions[key])
	}
	return reports
}

func (s *Store) mergeKey(key string, incoming []Version) *MergeReport {
	rep := &MergeReport{Added: []string{}, Duplicate: []string{}, Pruned: []string{}, PrunedIncoming: []string{}}
	local := s.data[key]
	localIDs := make(map[string]bool, len(local))
	for _, v := range local {
		localIDs[v.ID] = true
	}

	type cand struct {
		v        Version
		incoming bool
	}
	candidates := make([]cand, 0, len(local)+len(incoming))
	// knownDup are incoming IDs already present locally (possibly since
	// evolved). They are acknowledged as duplicates and NEVER enter the
	// candidate union, so a replay cannot prune or resurrect anything.
	knownDup := make(map[string]bool)
	seenIncoming := make(map[string]bool, len(incoming))
	for _, v := range incoming {
		if localIDs[v.ID] || seenIncoming[v.ID] {
			rep.Duplicate = append(rep.Duplicate, v.ID)
			knownDup[v.ID] = true
			continue
		}
		seenIncoming[v.ID] = true
		rep.Added = append(rep.Added, v.ID)
		candidates = append(candidates, cand{v: v, incoming: true})
	}
	_ = knownDup
	for _, v := range local {
		candidates = append(candidates, cand{v: v})
	}

	survivors := make([]Version, 0, len(candidates))
	for i, c := range candidates {
		dominated := false
		for j, other := range candidates {
			if i == j {
				continue
			}
			cmp := vclock.Compare(c.v.Clock, other.v.Clock)
			if cmp == vclock.Before || cmp == vclock.EqualRel {
				if cmp == vclock.EqualRel && c.v.ID < other.v.ID {
					continue
				}
				dominated = true
				break
			}
		}
		if dominated {
			if c.incoming {
				// A genuinely new incoming version dominated by local state.
				rep.PrunedIncoming = append(rep.PrunedIncoming, c.v.ID)
				rep.Added = removeID(rep.Added, c.v.ID)
			} else {
				rep.Pruned = append(rep.Pruned, c.v.ID)
			}
			continue
		}
		survivors = append(survivors, c.v)
	}
	sortVersions(survivors)
	if len(survivors) > 0 {
		s.data[key] = survivors
	} else {
		delete(s.data, key)
	}
	for _, sl := range []*[]string{&rep.Added, &rep.Duplicate, &rep.Pruned, &rep.PrunedIncoming} {
		sort.Strings(*sl)
	}
	return rep
}

// Snapshot returns an immutable copy of the whole store for replication.
func (s *Store) Snapshot() Snapshot {
	snap := Snapshot{Versions: make(map[string][]Version, len(s.data))}
	for k, versions := range s.data {
		cp := make([]Version, len(versions))
		for i, v := range versions {
			cp[i] = Version{
				ID:     v.ID,
				Value:  append(json.RawMessage(nil), v.Value...),
				Clock:  vclock.Copy(v.Clock),
				Author: v.Author,
			}
		}
		sortVersions(cp)
		snap.Versions[k] = cp
	}
	return snap
}

func sortVersions(vs []Version) {
	sort.Slice(vs, func(i, j int) bool { return vs[i].ID < vs[j].ID })
}

func removeID(xs []string, id string) []string {
	out := xs[:0]
	for _, x := range xs {
		if x != id {
			out = append(out, x)
		}
	}
	return out
}
