// Package orset implements an Observed-Remove Set (OR-Set / OR-Set CRDT).
//
// An OR-Set maps every element to the set of unique tags that added it.
// An element is a member iff it has at least one tag that has not been
// tombstoned. A remove records the set of tags it has OBSERVED for the
// element into the tombstone set; later additions get fresh unique tags and
// therefore survive (add-wins against a concurrent remove).
//
// The merge is a set union on both the add map and the tombstone set, which
// is commutative, associative and idempotent: any number of replicas, any
// message reordering, duplicates, retries and offline periods all converge.
package orset

import (
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"sort"
	"sync"
)

// Op is an individual mutation that can be replicated over an
// operation-based (cmrdt) channel. The same Op applied any number of times,
// in any order relative to other ops, yields the same state.
type Op struct {
	Type    string   `json:"type"`           // "add" or "remove"
	Element string   `json:"element"`        // affected element
	Tags    []string `json:"tags,omitempty"` // add: one fresh tag; remove: observed tags
}

// State is the serializable payload exchanged between replicas. Tags within
// an element are sorted and unique so that State is deterministic for tests.
type State struct {
	// Add[element] = tags that have ever added this element.
	Add map[string][]string `json:"add"`
	// Tombstones[element] = tags removed (observed at some remove site).
	Tombstones map[string][]string `json:"tombstones"`
}

// ORSet is a concurrency-safe OR-Set replica state.
type ORSet struct {
	mu        sync.Mutex
	replicaID string
	counter   uint64
	add       map[string]map[string]struct{}
	dead      map[string]map[string]struct{}
}

// New creates a replica with a replica-unique ID (used for tag minting).
func New(replicaID string) *ORSet {
	return &ORSet{
		replicaID: replicaID,
		add:       map[string]map[string]struct{}{},
		dead:      map[string]map[string]struct{}{},
	}
}

// ReplicaID returns the id this replica mints tags with.
func (s *ORSet) ReplicaID() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.replicaID
}

// randomSuffix returns 16 bytes of crypto randomness as hex. Combined with
// replicaID + a monotonic per-replica counter it makes a globally unique tag
// even across restarts on different machines.
func randomSuffix() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		// crypto/rand failing is not recoverable; the process cannot mint
		// safe unique tags without it.
		panic(fmt.Sprintf("orset: crypto/rand failed: %v", err))
	}
	return hex.EncodeToString(b[:])
}

// mints a fresh globally-unique tag. Callers must hold s.mu.
func (s *ORSet) mintTag() string {
	s.counter++
	return fmt.Sprintf("%s-%d-%s", s.replicaID, s.counter, randomSuffix())
}

// Add attaches a fresh unique tag to element and returns the generated tag
// (which can be replicated to peers inside an add Op).
func (s *ORSet) Add(element string) string {
	s.mu.Lock()
	defer s.mu.Unlock()
	tag := s.mintTag()
	tags := s.add[element]
	if tags == nil {
		tags = map[string]struct{}{}
		s.add[element] = tags
	}
	tags[tag] = struct{}{}
	return tag
}

// Remove tombstones every tag currently observed for element. It returns the
// observed tags and the corresponding remove Op (empty tags means the
// element was not present, so the op is a harmless no-op that need not be
// sent, though sending it is safe).
func (s *ORSet) Remove(element string) ([]string, Op) {
	s.mu.Lock()
	defer s.mu.Unlock()
	observed := make([]string, 0, len(s.add[element]))
	for t := range s.add[element] {
		observed = append(observed, t)
	}
	sort.Strings(observed)
	if len(observed) > 0 {
		dead := s.dead[element]
		if dead == nil {
			dead = map[string]struct{}{}
			s.dead[element] = dead
		}
		for _, t := range observed {
			dead[t] = struct{}{}
		}
	}
	return observed, Op{Type: "remove", Element: element, Tags: observed}
}

// Lookup reports whether element is currently in the set.
func (s *ORSet) Lookup(element string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.liveCountLocked(element) > 0
}

// Values returns the current elements, sorted.
func (s *ORSet) Values() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]string, 0, len(s.add))
	for e := range s.add {
		if s.liveCountLocked(e) > 0 {
			out = append(out, e)
		}
	}
	sort.Strings(out)
	return out
}

// liveCountLocked counts add tags not yet tombstoned. Caller holds s.mu.
func (s *ORSet) liveCountLocked(element string) int {
	n := 0
	for t := range s.add[element] {
		if _, isDead := s.dead[element][t]; !isDead {
			n++
		}
	}
	return n
}

// Snapshot returns a deep, deterministic copy of the full state for
// replication or inspection.
func (s *ORSet) Snapshot() State {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.snapshotLocked()
}

func (s *ORSet) snapshotLocked() State {
	st := State{
		Add:        make(map[string][]string, len(s.add)),
		Tombstones: make(map[string][]string, len(s.dead)),
	}
	for e, tags := range s.add {
		st.Add[e] = sortedCopy(tags)
	}
	for e, tags := range s.dead {
		st.Tombstones[e] = sortedCopy(tags)
	}
	return st
}

// Merge joins another replica's state into this one via set union. This is
// the state-based replication entry point; it is a join in a join-semilattice
// and therefore safe under reordering, duplication and partial delivery.
func (s *ORSet) Merge(other State) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for e, tags := range other.Add {
		dst := s.add[e]
		if dst == nil {
			dst = map[string]struct{}{}
			s.add[e] = dst
		}
		for _, t := range tags {
			dst[t] = struct{}{}
		}
	}
	for e, tags := range other.Tombstones {
		dst := s.dead[e]
		if dst == nil {
			dst = map[string]struct{}{}
			s.dead[e] = dst
		}
		for _, t := range tags {
			dst[t] = struct{}{}
		}
	}
}

// ApplyOp applies a single replicated add/remove operation. It is the
// op-based replication entry point and is intentionally idempotent:
//   - add: union the tag into the add set;
//   - remove: union the observed tags into BOTH the tombstone set and the add
//     set. Recording removed tags in the add set as well makes a remove safe
//     even if its matching add has not arrived yet — when that add later
//     lands, the tag is already tombstoned, so the element does not
//     resurrect. The tag pair (add[tag], dead[tag]) is itself an OR-Set cell.
func (s *ORSet) ApplyOp(op Op) error {
	if op.Element == "" {
		return errors.New("orset: op has empty element")
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	switch op.Type {
	case "add":
		if len(op.Tags) == 0 {
			return errors.New("orset: add op without tag")
		}
		dst := s.add[op.Element]
		if dst == nil {
			dst = map[string]struct{}{}
			s.add[op.Element] = dst
		}
		for _, t := range op.Tags {
			dst[t] = struct{}{}
		}
		return nil
	case "remove":
		if len(op.Tags) == 0 {
			// remove of nothing: a no-op that remains safe to replicate.
			return nil
		}
		addDst := s.add[op.Element]
		if addDst == nil {
			addDst = map[string]struct{}{}
			s.add[op.Element] = addDst
		}
		deadDst := s.dead[op.Element]
		if deadDst == nil {
			deadDst = map[string]struct{}{}
			s.dead[op.Element] = deadDst
		}
		for _, t := range op.Tags {
			addDst[t] = struct{}{}
			deadDst[t] = struct{}{}
		}
		return nil
	default:
		return fmt.Errorf("orset: unknown op type %q", op.Type)
	}
}

// GCTags permanently purges the given tags from BOTH the tombstone set and the
// add history. It must only be called once the caller is certain every
// surviving replica already knows these tags are dead (see package gc). After
// GC a tag is gone everywhere; an undelivered add carrying that exact tag is
// the only thing that could "revive" it — which is why GC requires cluster
// wide observation. Purge is itself idempotent.
func (s *ORSet) GCTags(tags map[string][]string) int {
	s.mu.Lock()
	defer s.mu.Unlock()
	n := 0
	for e, ts := range tags {
		for _, t := range ts {
			if _, ok := s.dead[e][t]; ok {
				delete(s.dead[e], t)
				delete(s.add[e], t)
				n++
			}
		}
		if len(s.add[e]) == 0 {
			delete(s.add, e)
		}
		if len(s.dead[e]) == 0 {
			delete(s.dead, e)
		}
	}
	return n
}

// Stats holds counters, mainly for tests and the /debug endpoint.
type Stats struct {
	Elements      int `json:"elements"`
	AddTags       int `json:"addTags"`
	TombstoneTags int `json:"tombstoneTags"`
}

// Stats returns structural counters for the replica.
func (s *ORSet) Stats() Stats {
	s.mu.Lock()
	defer s.mu.Unlock()
	var st Stats
	for e := range s.add {
		st.AddTags += len(s.add[e])
		if s.liveCountLocked(e) > 0 {
			st.Elements++
		}
	}
	for _, tags := range s.dead {
		st.TombstoneTags += len(tags)
	}
	return st
}

// Equal compares two states as sets (independent of slice order).
func Equal(a, b State) bool {
	if len(a.Add) != len(b.Add) || len(a.Tombstones) != len(b.Tombstones) {
		return false
	}
	for e, tags := range a.Add {
		if !sameSet(tags, b.Add[e]) {
			return false
		}
	}
	for e, tags := range a.Tombstones {
		if !sameSet(tags, b.Tombstones[e]) {
			return false
		}
	}
	return true
}

func sameSet(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	seen := make(map[string]struct{}, len(b))
	for _, t := range b {
		seen[t] = struct{}{}
	}
	for _, t := range a {
		if _, ok := seen[t]; !ok {
			return false
		}
	}
	return true
}

func sortedCopy(m map[string]struct{}) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}
