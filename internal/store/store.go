// Package store holds the replicated key/value state.
//
// The store is an in-memory, mutex-protected map of Entry records. Every
// mutating API bumps an epoch counter; a sync round pins an immutable
// Snapshot and is only allowed to apply its result back while the store's
// epoch still matches the snapshot's epoch. That is the optimistic
// concurrency control that lets a sync detect that the tree root moved
// underneath it and retry with a fresh, consistent snapshot.
//
// Deleted keys are represented by tombstone entries (Deleted=true) which
// participate in hashing and replication; this package never garbage
// collects them (see README "Known limitations").
package store

import (
	"encoding/json"
	"sort"
	"sync"
	"time"

	"github.com/example/merklekv/internal/hlc"
)

// Entry is one versioned key record. A tombstone is an entry with
// Deleted == true and a nil Value.
type Entry struct {
	Key     string        `json:"key"`
	Value   []byte        `json:"value,omitempty"`
	Ver     hlc.Timestamp `json:"ver"`
	Origin  string        `json:"origin"` // id of the replica that wrote this version
	Deleted bool          `json:"deleted,omitempty"`
}

// Clone returns a deep copy of the entry.
func (e Entry) Clone() Entry {
	var v []byte
	if e.Value != nil {
		v = append([]byte(nil), e.Value...)
	}
	return Entry{Key: e.Key, Value: v, Ver: e.Ver, Origin: e.Origin, Deleted: e.Deleted}
}

// Snapshot is an immutable point-in-time view of the store.
type Snapshot struct {
	ID      string
	Epoch   int64
	Created time.Time
	Records int   // number of records (including tombstones)
	Bytes   int64 // sum of key+value byte lengths, recorded once at creation
	// WireBytes is the JSON-encoded size of the record set: the cost a naive
	// "ship everything" reconciliation would pay on the wire. It is the
	// baseline the Merkle exchange is compared against in sync stats.
	WireBytes int64
	entries   []Entry // sorted by key, values copied
}

// Entries returns the snapshot entries in key order. Callers must not mutate
// the returned slice or its byte slices.
func (s *Snapshot) Entries() []Entry { return s.entries }

// Config configures a Store.
type Config struct {
	ReplicaID    string
	SnapshotTTL  time.Duration // default 30s
	MaxSnapshots int           // hard cap, oldest snapshot evicted first; default 64
}

// Store is the concurrent-safe, versioned key/value state of one replica.
type Store struct {
	mu       sync.RWMutex
	id       string
	data     map[string]Entry
	epoch    int64
	clock    *hlc.Clock
	snaps    map[string]*Snapshot
	snapTTL  time.Duration
	maxSnaps int
}

// New builds a store.
func New(cfg Config) *Store {
	if cfg.SnapshotTTL <= 0 {
		cfg.SnapshotTTL = 30 * time.Second
	}
	if cfg.MaxSnapshots <= 0 {
		cfg.MaxSnapshots = 64
	}
	return &Store{
		id:       cfg.ReplicaID,
		data:     make(map[string]Entry),
		clock:    hlc.NewClock(nil),
		snaps:    make(map[string]*Snapshot),
		snapTTL:  cfg.SnapshotTTL,
		maxSnaps: cfg.MaxSnapshots,
	}
}

// ReplicaID returns the configured replica id.
func (s *Store) ReplicaID() string { return s.id }

// Clock exposes the HLC (e.g. for tests to observe foreign timestamps).
func (s *Store) Clock() *hlc.Clock { return s.clock }

// Epoch returns the current mutation epoch.
func (s *Store) Epoch() int64 {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.epoch
}

// Get returns a clone of the live entry for key, or false if absent or
// tombstoned.
func (s *Store) Get(key string) (Entry, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	e, ok := s.data[key]
	if !ok || e.Deleted {
		return Entry{}, false
	}
	return e.Clone(), true
}

// Put writes a new live version and bumps the epoch.
func (s *Store) Put(key string, value []byte) Entry {
	s.mu.Lock()
	defer s.mu.Unlock()
	ver := s.clock.Tick()
	e := Entry{Key: key, Value: append([]byte(nil), value...), Ver: ver, Origin: s.id}
	s.data[key] = e
	s.epoch++
	return e.Clone()
}

// Delete writes a tombstone (or bumps an existing one) and the epoch.
func (s *Store) Delete(key string) (Entry, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	_, existed := s.data[key]
	ver := s.clock.Tick()
	e := Entry{Key: key, Ver: ver, Origin: s.id, Deleted: true}
	s.data[key] = e
	s.epoch++
	return e.Clone(), existed
}

// ApplyResult is the outcome of ApplyEntries.
type ApplyResult struct {
	Epoch          int64
	Applied        int // records that replaced the local winner
	Unchanged      int // records skipped because local version was newer/equal
	LatestObserved hlc.Timestamp
}

// ApplyEntries merges replicated entries into the store.
//
// The merge is last-writer-wins per key: higher (ver, origin) wins, where
// origin string order breaks exact timestamp ties. The HLC is observed for
// every applied timestamp. The whole batch applies only when expectedEpoch
// matches the store's current epoch (-1 disables the guard); on mismatch
// nothing is touched and Changed=false is returned with the current epoch.
func (s *Store) ApplyEntries(expectedEpoch int64, in []Entry) (ApplyResult, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if expectedEpoch >= 0 && expectedEpoch != s.epoch {
		return ApplyResult{Epoch: s.epoch}, false
	}
	res := ApplyResult{Epoch: s.epoch}
	latest := hlc.Timestamp{}
	for _, e0 := range in {
		e := e0.Clone()
		if cur, ok := s.data[e.Key]; ok {
			if !newerEntry(e, cur) {
				res.Unchanged++
				if cur.Ver.Compare(latest) > 0 {
					latest = cur.Ver
				}
				continue
			}
		}
		if e.Ver.Compare(latest) > 0 {
			latest = e.Ver
		}
		s.data[e.Key] = e
		res.Applied++
	}
	if res.Applied > 0 {
		s.epoch++
		res.Epoch = s.epoch
	}
	s.clock.Observe(latest)
	res.LatestObserved = latest
	return res, true
}

// newerEntry reports whether candidate a should replace incumbent b.
// Higher ver wins; on exact ver tie, the lexicographically larger origin
// wins (deterministic, symmetric on both replicas).
func newerEntry(a, b Entry) bool {
	if c := a.Ver.Compare(b.Ver); c != 0 {
		return c > 0
	}
	return a.Origin > b.Origin
}

// Seed replaces the whole store state directly from a batch WITHOUT bumping
// the epoch and WITHOUT handing out HLC timestamps. It exists for tests and
// the /admin/seed demo helper, which need deterministically versioned
// initial data. It also invalidates every existing snapshot, since their
// contents no longer reflect the store.
func (s *Store) Seed(entries []Entry) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.data = make(map[string]Entry, len(entries))
	for _, e0 := range entries {
		e := e0.Clone()
		if e.Origin == "" {
			e.Origin = s.id
		}
		s.data[e.Key] = e
		s.clock.Observe(e.Ver)
	}
	s.snaps = make(map[string]*Snapshot)
}

// Snapshot takes (or returns an existing) immutable view. The id is stable
// for the same (epoch) so that the sync endpoints can refer to snapshots
// across requests without a proliferation of copies.
func (s *Store) Snapshot() *Snapshot {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.evictExpiredLocked()
	for _, sn := range s.snaps {
		if sn.Epoch == s.epoch {
			return sn
		}
	}
	entries := make([]Entry, 0, len(s.data))
	var nbytes int64
	for _, e := range s.data {
		entries = append(entries, e.Clone())
		nbytes += int64(len(e.Key) + len(e.Value))
	}
	sort.Slice(entries, func(i, j int) bool { return entries[i].Key < entries[j].Key })
	wire, err := json.Marshal(entries)
	if err != nil {
		// Entries contain only strings, bytes and integers; this cannot fail.
		panic(err)
	}
	sn := &Snapshot{
		ID:        newSnapshotID(),
		Epoch:     s.epoch,
		Created:   time.Now(),
		Records:   len(entries),
		Bytes:     nbytes,
		WireBytes: int64(len(wire)),
		entries:   entries,
	}
	s.snaps[sn.ID] = sn
	s.evictOverflowLocked()
	return sn
}

// GetSnapshot looks up a retained snapshot by id.
func (s *Store) GetSnapshot(id string) (*Snapshot, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.evictExpiredLocked()
	sn, ok := s.snaps[id]
	return sn, ok
}

// ReleaseSnapshot drops a retained snapshot. Unknown ids are a no-op.
func (s *Store) ReleaseSnapshot(id string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.snaps, id)
}

// EvictAllSnapshots is a test/admin helper: it simulates the server losing
// every pinned snapshot (TTL expiry / capacity pressure), which forces the
// sync client to restart its round with 404 -> fresh snapshot.
func (s *Store) EvictAllSnapshots() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.snaps = make(map[string]*Snapshot)
}

// SortedLiveEntries returns clones of all non-tombstone entries in key order.
// Used by tests to compare converged state.
func (s *Store) SortedLiveEntries() []Entry {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make([]Entry, 0, len(s.data))
	for _, e := range s.data {
		if !e.Deleted {
			out = append(out, e.Clone())
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Key < out[j].Key })
	return out
}

// AllEntries returns clones of every record including tombstones, key sorted.
func (s *Store) AllEntries() []Entry {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make([]Entry, 0, len(s.data))
	for _, e := range s.data {
		out = append(out, e.Clone())
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Key < out[j].Key })
	return out
}

func (s *Store) evictExpiredLocked() {
	now := time.Now()
	for id, sn := range s.snaps {
		if now.Sub(sn.Created) >= s.snapTTL {
			delete(s.snaps, id)
		}
	}
}

// evictOverflowLocked enforces the snapshot cap, dropping the oldest first.
func (s *Store) evictOverflowLocked() {
	for len(s.snaps) > s.maxSnaps {
		var oldestID string
		var oldestAt time.Time
		for id, sn := range s.snaps {
			if oldestID == "" || sn.Created.Before(oldestAt) {
				oldestID, oldestAt = id, sn.Created
			}
		}
		delete(s.snaps, oldestID)
	}
}
