// Package orset implements an Observed-Remove Set (OR-Set / OR-Set CRDT).
//
// An add mints a globally-unique tag bound to an element; the element belongs
// to the set while at least one of its tags is live (not yet observed-removed).
// A remove only tombstones tags the removing replica has *observed* at the
// moment of removal, so concurrent adds that the remover has not yet seen are
// preserved. Merge is set union on both the tag set and the tombstone set,
// which makes it commutative, associative and idempotent.
package orset

import (
	"fmt"
	"sort"
	"strings"
)

// Tag is a globally-unique add tag. Origin is the replica that minted it and
// Seq is that replica's monotonic counter, so the pair (Origin, Seq) is unique
// without any coordination. Counter minting is deterministic, which keeps the
// whole simulation reproducible (no random UUIDs).
type Tag struct {
	Origin string `json:"origin"`
	Seq    uint64 `json:"seq"`
}

// String renders a tag as "origin:seq".
func (t Tag) String() string { return fmt.Sprintf("%s:%d", t.Origin, t.Seq) }

// State is one replica's OR-Set state: the full add-tag map plus the set of
// tombstoned tags. Adds maps element -> tags minted for it (every tag ever
// observed, live or removed). Removed is a tombstone set.
//
// The zero value is a valid, empty set.
type State struct {
	Adds    map[string][]Tag `json:"adds"`
	Removed map[Tag]struct{} `json:"removed"`
}

// New returns an empty OR-Set state.
func New() *State {
	return &State{
		Adds:    make(map[string][]Tag),
		Removed: make(map[Tag]struct{}),
	}
}

// Clone returns a deep copy of s.
func (s *State) Clone() *State {
	c := New()
	for e, tags := range s.Adds {
		cp := make([]Tag, len(tags))
		copy(cp, tags)
		c.Adds[e] = cp
	}
	for t := range s.Removed {
		c.Removed[t] = struct{}{}
	}
	return c
}

// Add mints tag (origin, seq) and binds it to element. The caller guarantees
// that seq is unique per origin (a monotonic counter on the origin replica).
// Returns the minted tag so the simulator can record it in its trace.
func (s *State) Add(origin string, seq uint64, element string) Tag {
	t := Tag{Origin: origin, Seq: seq}
	s.Adds[element] = append(s.Adds[element], t)
	return t
}

// Remove tombstones, for element, exactly the tags currently visible on this
// replica. Tags that have never been observed here — e.g. tags minted by a
// concurrent add still in flight — are left untouched. This is the
// "observed-remove" rule: removing what you cannot see cannot remove it.
//
// Returns the tombstoned tags (possibly empty: removing an absent element is a
// no-op by design).
func (s *State) Remove(element string) []Tag {
	var removed []Tag
	for _, t := range s.Adds[element] {
		if _, dead := s.Removed[t]; dead {
			continue
		}
		s.Removed[t] = struct{}{}
		removed = append(removed, t)
	}
	return removed
}

// Lookup reports whether element is currently in the set: at least one of its
// tags is live (present in Adds but absent from Removed).
func (s *State) Lookup(element string) bool {
	for _, t := range s.Adds[element] {
		if _, dead := s.Removed[t]; !dead {
			return true
		}
	}
	return false
}

// Values returns the sorted list of elements currently in the set.
func (s *State) Values() []string {
	var out []string
	for e := range s.Adds {
		if s.Lookup(e) {
			out = append(out, e)
		}
	}
	sort.Strings(out)
	return out
}

// Tombstones returns the set of observed-removed tags on this replica.
func (s *State) Tombstones() map[Tag]struct{} { return s.Removed }

// ContainsTag reports whether this state knows tag t at all.
func (s *State) ContainsTag(t Tag) bool {
	for _, tags := range s.Adds {
		for _, x := range tags {
			if x == t {
				return true
			}
		}
	}
	return false
}

// Merge unions other into the receiver (in place) and returns the receiver.
//
//	Adds'    = Adds ∪ Adds_other      (union of add tags per element)
//	Removed' = Removed ∪ Removed_other (union of tombstones)
//
// Both components are plain sets under union, therefore Merge is commutative,
// associative and idempotent. The receiver becomes the join; other is never
// mutated (Merge merges other *into* s).
func (s *State) Merge(other *State) *State {
	if s.Adds == nil {
		s.Adds = make(map[string][]Tag)
	}
	if s.Removed == nil {
		s.Removed = make(map[Tag]struct{})
	}
	for e, tags := range other.Adds {
		have := tagIndex(s.Adds[e])
		for _, t := range tags {
			if !have[t] {
				s.Adds[e] = append(s.Adds[e], t)
				have[t] = true
			}
		}
	}
	for t := range other.Removed {
		s.Removed[t] = struct{}{}
	}
	return s
}

// Equal reports structural equality (same tag map and same tombstone set),
// independent of map/slice iteration order.
func (s *State) Equal(o *State) bool {
	if len(s.Adds) != len(o.Adds) {
		return false
	}
	if len(s.Removed) != len(o.Removed) {
		return false
	}
	for e, tags := range s.Adds {
		otags, ok := o.Adds[e]
		if !ok || len(tags) != len(otags) {
			return false
		}
		a := tagSet(tags)
		b := tagSet(otags)
		if len(a) != len(b) {
			return false
		}
		for t := range a {
			if _, ok := b[t]; !ok {
				return false
			}
		}
	}
	for t := range s.Removed {
		if _, ok := o.Removed[t]; !ok {
			return false
		}
	}
	return true
}

// StableTags returns the tags of element that every replica named in replicas
// has observed (i.e. the tag appears in every given state's Adds). Such tags
// are safe to reclaim after every replica has also observed their tombstones.
//
// Reclamation stability precondition (see README "墓碑回收"): a tag and its
// tombstone may only be discarded after a quorum-free all-replica barrier in
// which every replica that could ever merge back has acknowledged knowing
// both the tag and its removal. This helper tests the *observation* part of
// that precondition; Reclaim performs the actual deletion only for tags that
// are tombstoned here as well.
func StableTags(element string, replicas ...*State) []Tag {
	if len(replicas) == 0 {
		return nil
	}
	var candidates map[Tag]bool
	for _, r := range replicas {
		known := tagIndex(r.Adds[element])
		if candidates == nil {
			candidates = known
			continue
		}
		for t := range candidates {
			if !known[t] {
				delete(candidates, t)
			}
		}
	}
	var out []Tag
	for t := range candidates {
		out = append(out, t)
	}
	sortTags(out)
	return out
}

// Reclaim discards, from this state, tags listed in tags if (and only if) the
// tag is tombstoned in every replica named in witnesses. The witness check is
// the stability barrier: the caller must pass the states of ALL replicas that
// might ever be merged with this one afterwards (including replicas that are
// currently partitioned, stopped, or could rejoin with an old snapshot).
//
// If that condition cannot be guaranteed the caller must not reclaim — a tag
// reintroduced later by a straggler that never saw the tombstone would
// resurrect the element. Reclaim returns the number of tags discarded.
func (s *State) Reclaim(tags []Tag, witnesses ...*State) int {
	n := 0
	for _, t := range tags {
		if _, deadHere := s.Removed[t]; !deadHere {
			continue // only ever reclaim removed tags
		}
		// The receiver's own tombstone check was just performed above; it
		// must not also appear in witnesses (its state changes during this
		// call, which would make a later check read a mid-reclaim snapshot).
		safe := true
		for _, w := range witnesses {
			if w == s {
				continue
			}
			if _, deadThere := w.Removed[t]; !deadThere {
				safe = false
				break
			}
		}
		if !safe {
			continue
		}
		// Remove t from the element it is bound to.
		for e, bound := range s.Adds {
			if !tagIndex(bound)[t] {
				continue
			}
			s.Adds[e] = removeTag(s.Adds[e], t)
			if len(s.Adds[e]) == 0 {
				delete(s.Adds, e)
			}
			break
		}
		delete(s.Removed, t)
		n++
	}
	return n
}

// Canonical renders the state deterministically (sorted elements and tags),
// useful for traces and golden comparisons.
func (s *State) Canonical() string {
	elems := make([]string, 0, len(s.Adds))
	for e := range s.Adds {
		elems = append(elems, e)
	}
	sort.Strings(elems)
	var b strings.Builder
	b.WriteString("adds={")
	for i, e := range elems {
		if i > 0 {
			b.WriteByte(',')
		}
		tags := append([]Tag(nil), s.Adds[e]...)
		sortTags(tags)
		strs := make([]string, len(tags))
		for j, t := range tags {
			strs[j] = t.String()
		}
		fmt.Fprintf(&b, "%s:[%s]", e, strings.Join(strs, ","))
	}
	b.WriteString("} removed={")
	rt := make([]Tag, 0, len(s.Removed))
	for t := range s.Removed {
		rt = append(rt, t)
	}
	sortTags(rt)
	strs := make([]string, len(rt))
	for j, t := range rt {
		strs[j] = t.String()
	}
	b.WriteString(strings.Join(strs, ","))
	b.WriteByte('}')
	return b.String()
}

func tagIndex(tags []Tag) map[Tag]bool {
	m := make(map[Tag]bool, len(tags))
	for _, t := range tags {
		m[t] = true
	}
	return m
}

func tagSet(tags []Tag) map[Tag]struct{} {
	m := make(map[Tag]struct{}, len(tags))
	for _, t := range tags {
		m[t] = struct{}{}
	}
	return m
}

func removeTag(tags []Tag, t Tag) []Tag {
	out := make([]Tag, 0, len(tags)) // fresh array: never alias the caller's slice
	for _, x := range tags {
		if x != t {
			out = append(out, x)
		}
	}
	return out
}

func sortTags(tags []Tag) {
	sort.Slice(tags, func(i, j int) bool {
		if tags[i].Origin != tags[j].Origin {
			return tags[i].Origin < tags[j].Origin
		}
		return tags[i].Seq < tags[j].Seq
	})
}
