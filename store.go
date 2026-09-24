package main

import (
	"errors"
	"fmt"
	"sort"
	"sync"
)

var (
	ErrReplicaNotFound = errors.New("replica not found")
	ErrReplicaExists   = errors.New("replica already exists")
	ErrEmptyReplicaID  = errors.New("replica id must not be empty")
	ErrKeyNotFound     = errors.New("key not found")
	ErrVersionNotFound = errors.New("context version not found for key")
	ErrInvalidVersion  = errors.New("invalid version")
	ErrSameReplica     = errors.New("source and destination replicas must differ")
)

// Replica is one independent copy of the register. Replicas do not talk to
// each other on their own: writes stay local until a message is delivered or a
// sync is explicitly requested, which models a partitioned/distributed system
// inside one process.
type Replica struct {
	id    string
	clock Clock                // everything this replica knows about
	keys  map[string][]Version // key -> antichain of sibling versions
}

// Snapshot is the serializable full state of one replica. It is both the wire
// format of replica-to-replica sync and the body of GET /replicas/{id}.
type Snapshot struct {
	Replica string               `json:"replica"`
	Clock   Clock                `json:"clock"`
	Keys    map[string][]Version `json:"keys"`
}

// SyncKeySummary counts what an inbound merge changed for one key.
type SyncKeySummary struct {
	Key           string `json:"key"`
	Accepted      int    `json:"accepted"`
	Duplicate     int    `json:"duplicate"`
	Superseded    int    `json:"superseded"`
	VersionsAfter int    `json:"versions_after"`
}

// SyncReport is the result of one (or, in two-way mode, two) sync operation.
type SyncReport struct {
	From string `json:"from"`
	To   string `json:"to"`
	Mode string `json:"mode"`
	// Outbound summarizes the effect of From's state arriving at To.
	Outbound []SyncKeySummary `json:"outbound"`
	// Inbound is present only in two-way mode and summarizes the reverse
	// direction (To's state arriving at From).
	Inbound []SyncKeySummary `json:"inbound,omitempty"`
}

// Store holds all replicas. A single mutex serializes every mutating
// operation, including sync, which makes the simulation race-free and removes
// any possibility of lock-ordering deadlock between peers.
type Store struct {
	mu       sync.Mutex
	replicas map[string]*Replica
}

// NewStore creates an empty store.
func NewStore() *Store {
	return &Store{replicas: make(map[string]*Replica)}
}

func validateVersion(v Version) error {
	if v.ID == "" {
		return fmt.Errorf("%w: version id must not be empty", ErrInvalidVersion)
	}
	if v.Origin == "" {
		return fmt.Errorf("%w: version %q has empty origin", ErrInvalidVersion, v.ID)
	}
	if len(v.Clock) == 0 {
		return fmt.Errorf("%w: version %q has empty clock", ErrInvalidVersion, v.ID)
	}
	for replica, n := range v.Clock {
		if replica == "" {
			return fmt.Errorf("%w: version %q clock has empty replica entry", ErrInvalidVersion, v.ID)
		}
		if n <= 0 {
			return fmt.Errorf("%w: version %q clock entry %q must be positive", ErrInvalidVersion, v.ID, replica)
		}
	}
	return nil
}

// merge is the single implementation behind both a normal write and an
// explicit contextual merge.
//
// contextIDs selects the sibling versions the client declares to have
// "joined" into the new value (the merge context):
//
//   - nil (a plain PUT write): the replica writes on top of every event it
//     knows, so the new version causally overwrites the whole antichain;
//   - non-empty (an explicit merge): the new version descends ONLY from the
//     named context versions (Dynamo-style client context). A sibling the
//     replica happens to know about but which the client left out of the
//     context is treated as not-yet-merged and stays alive whenever its clock
//     is incomparable with the merge clock.
//
// The local component of the new clock is allocated from the replica-wide
// event counter (r.clock[r.id]), not from the base clock, so version ids stay
// unique even when a narrow merge clock omits events the replica has seen.
func (r *Replica) merge(key, value string, contextIDs []string) (Version, []Version, error) {
	current := r.keys[key]
	byID := make(map[string]Version, len(current))
	for _, v := range current {
		byID[v.ID] = v
	}

	var base Clock
	switch {
	case len(contextIDs) > 0:
		clocks := make([]Clock, 0, len(contextIDs))
		seen := make(map[string]struct{}, len(contextIDs))
		for _, id := range contextIDs {
			if _, dup := seen[id]; dup {
				return Version{}, nil, fmt.Errorf("%w: %q listed more than once", ErrInvalidVersion, id)
			}
			seen[id] = struct{}{}
			v, ok := byID[id]
			if !ok {
				return Version{}, nil, fmt.Errorf("%w: %q", ErrVersionNotFound, id)
			}
			clocks = append(clocks, v.Clock)
		}
		base = ClockJoin(clocks...)
	default:
		// Everything stored is dominated by r.clock; join the stored clocks
		// explicitly so the overwrite intent is visible at the call site.
		clocks := make([]Clock, 0, len(current)+1)
		clocks = append(clocks, r.clock)
		for _, v := range current {
			clocks = append(clocks, v.Clock)
		}
		base = ClockJoin(clocks...)
	}

	counter := r.clock[r.id] + 1
	vc := base.Clone()
	vc[r.id] = counter
	newVersion := Version{
		ID:     fmt.Sprintf("%s-%d", r.id, counter),
		Origin: r.id,
		Clock:  vc,
		Value:  value,
	}

	// Record the event in the replica's knowledge even when the version
	// itself descends from a narrower context: r.clock must dominate every
	// event the replica generated or received.
	r.clock = ClockJoin(r.clock, vc)
	survivors, _ := MergeVersions(current, []Version{newVersion})
	r.keys[key] = survivors
	clocks := make([]Clock, 0, len(survivors))
	for _, v := range survivors {
		clocks = append(clocks, v.Clock)
	}
	r.clock = ClockJoin(append([]Clock{r.clock}, clocks...)...)
	return newVersion, cloneVersions(survivors), nil
}

// deliver applies versions received in a single point-to-point message
// (possibly retransmitted or delayed) to one key.
func (r *Replica) deliver(key string, incoming []Version) ([]Version, map[string]IncomingOutcome) {
	survivors, outcomes := MergeVersions(r.keys[key], incoming)
	r.keys[key] = survivors
	clocks := make([]Clock, 0, len(survivors)+1)
	clocks = append(clocks, r.clock)
	for _, v := range survivors {
		clocks = append(clocks, v.Clock)
	}
	r.clock = ClockJoin(clocks...)
	return cloneVersions(survivors), outcomes
}

// snapshot returns a deep copy of the replica state, safe to hand to another
// replica or serialize while mutations continue.
func (r *Replica) snapshot() Snapshot {
	keys := make(map[string][]Version, len(r.keys))
	for k, vs := range r.keys {
		keys[k] = cloneVersions(vs)
	}
	return Snapshot{Replica: r.id, Clock: r.clock.Clone(), Keys: keys}
}

// applySnapshot merges a peer snapshot into this replica (the receiving side
// of sync) and returns one summary per key present in the snapshot.
func (r *Replica) applySnapshot(s Snapshot) []SyncKeySummary {
	r.clock = ClockJoin(r.clock, s.Clock)
	names := make([]string, 0, len(s.Keys))
	for k := range s.Keys {
		names = append(names, k)
	}
	sort.Strings(names)

	summaries := make([]SyncKeySummary, 0, len(names))
	for _, k := range names {
		incoming := s.Keys[k]
		// Validate defensively: sync must never persist malformed state.
		valid := make([]Version, 0, len(incoming))
		for _, v := range incoming {
			if validateVersion(v) == nil {
				valid = append(valid, v)
			}
		}
		survivors, outcomes := MergeVersions(r.keys[k], valid)
		r.keys[k] = survivors

		summary := SyncKeySummary{Key: k, VersionsAfter: len(survivors)}
		for _, o := range outcomes {
			switch o {
			case OutcomeAccepted:
				summary.Accepted++
			case OutcomeDuplicate:
				summary.Duplicate++
			case OutcomeSuperseded:
				summary.Superseded++
			}
		}
		summaries = append(summaries, summary)

		clocks := make([]Clock, 0, len(survivors))
		for _, v := range survivors {
			clocks = append(clocks, v.Clock)
		}
		r.clock = ClockJoin(append([]Clock{r.clock}, clocks...)...)
	}
	return summaries
}

func cloneVersions(vs []Version) []Version {
	out := make([]Version, len(vs))
	for i, v := range vs {
		out[i] = v.Clone()
	}
	return out
}

// ---------------------------------------------------------------- store API

// Reset removes every replica.
func (s *Store) Reset() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.replicas = make(map[string]*Replica)
}

// CreateReplica adds a new empty replica.
func (s *Store) CreateReplica(id string) error {
	if id == "" {
		return ErrEmptyReplicaID
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.replicas[id]; ok {
		return fmt.Errorf("%w: %q", ErrReplicaExists, id)
	}
	s.replicas[id] = &Replica{id: id, clock: Clock{}, keys: make(map[string][]Version)}
	return nil
}

// DeleteReplica removes a replica.
func (s *Store) DeleteReplica(id string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.replicas[id]; !ok {
		return fmt.Errorf("%w: %q", ErrReplicaNotFound, id)
	}
	delete(s.replicas, id)
	return nil
}

// ReplicaIDs lists replica ids in lexicographic order.
func (s *Store) ReplicaIDs() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	ids := make([]string, 0, len(s.replicas))
	for id := range s.replicas {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	return ids
}

func (s *Store) getLocked(id string) (*Replica, error) {
	r, ok := s.replicas[id]
	if !ok {
		return nil, fmt.Errorf("%w: %q", ErrReplicaNotFound, id)
	}
	return r, nil
}

// ReadView is the JSON representation of a key read.
type ReadView struct {
	Replica  string    `json:"replica"`
	Key      string    `json:"key"`
	Clock    Clock     `json:"clock"`
	Versions []Version `json:"versions"`
}

// Read returns the sibling versions stored for a key on one replica.
func (s *Store) Read(replicaID, key string) (ReadView, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	r, err := s.getLocked(replicaID)
	if err != nil {
		return ReadView{}, err
	}
	vs, ok := r.keys[key]
	if !ok || len(vs) == 0 {
		return ReadView{}, fmt.Errorf("%w: %q", ErrKeyNotFound, key)
	}
	return ReadView{Replica: r.id, Key: key, Clock: r.clock.Clone(), Versions: cloneVersions(vs)}, nil
}

// Write performs a local causal-overwrite write (the new version implicitly
// supersedes all current siblings) and returns the created version alongside
// the surviving antichain.
func (s *Store) Write(replicaID, key, value string) (Version, []Version, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	r, err := s.getLocked(replicaID)
	if err != nil {
		return Version{}, nil, err
	}
	return r.merge(key, value, nil)
}

// Merge performs an explicit merge of the named context versions.
func (s *Store) Merge(replicaID, key, value string, contextIDs []string) (Version, []Version, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	r, err := s.getLocked(replicaID)
	if err != nil {
		return Version{}, nil, err
	}
	return r.merge(key, value, contextIDs)
}

// DeliverResult reports one explicit message delivery.
type DeliverResult struct {
	Replica   string                     `json:"replica"`
	Key       string                     `json:"key"`
	Outcomes  map[string]IncomingOutcome `json:"outcomes"`
	Survivors []Version                  `json:"survivors"`
}

// Deliver applies versions carried in one explicit message to one key of one
// replica. Duplicate ids are reported as such and never stored twice.
func (s *Store) Deliver(replicaID, key string, incoming []Version) (DeliverResult, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	r, err := s.getLocked(replicaID)
	if err != nil {
		return DeliverResult{}, err
	}
	for _, v := range incoming {
		if err := validateVersion(v); err != nil {
			return DeliverResult{}, err
		}
	}
	survivors, outcomes := r.deliver(key, cloneVersions(incoming))
	return DeliverResult{Replica: r.id, Key: key, Outcomes: outcomes, Survivors: survivors}, nil
}

// Snapshot returns a deep copy of one replica's full state.
func (s *Store) Snapshot(replicaID string) (Snapshot, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	r, err := s.getLocked(replicaID)
	if err != nil {
		return Snapshot{}, err
	}
	return r.snapshot(), nil
}

// Sync copies state from one replica to another (one-way) or in both
// directions (two-way). Both directions are evaluated from snapshots taken at
// the same instant under one lock, and both inbound merges run before the
// lock is released; the two antichain merges commute, so after a two-way sync
// both replicas hold exactly the same versions for every key.
func (s *Store) Sync(fromID, toID, mode string) (SyncReport, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	from, err := s.getLocked(fromID)
	if err != nil {
		return SyncReport{}, err
	}
	to, err := s.getLocked(toID)
	if err != nil {
		return SyncReport{}, err
	}
	if fromID == toID {
		return SyncReport{}, ErrSameReplica
	}

	fromSnap := from.snapshot()
	toSnap := to.snapshot()

	report := SyncReport{From: fromID, To: toID, Mode: mode, Outbound: []SyncKeySummary{}}
	report.Outbound = to.applySnapshot(fromSnap)
	if mode == SyncModeTwoWay {
		// Re-snapshot the source? No: the reverse merge only needs To's
		// pre-sync state, which is exactly toSnap. Applying it after the
		// forward merge is equivalent to merging both at once because
		// antichain union is associative and commutative.
		report.Inbound = from.applySnapshot(toSnap)
	}
	return report, nil
}

const (
	// SyncModeOneWay only propagates From's state to To.
	SyncModeOneWay = "one-way"
	// SyncModeTwoWay exchanges state in both directions.
	SyncModeTwoWay = "two-way"
)
