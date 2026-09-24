// Package store implements an in-memory versioned key/value store.
//
// Every live key carries a strictly increasing integer version. Deletes are
// recorded as tombstones (also versioned) so that LWW (last-writer-wins, higher
// version wins) reconciliation between two replicas can converge correctly.
//
// Each mutating operation advances a global, monotonically increasing revision
// number. The revision only ever goes up within an epoch; Reset() starts a new
// epoch, which is how tests/demo obtain a clean state without stale caches.
package store

import (
	"errors"
	"sync"
)

var ErrInvalidVersion = errors.New("version must be >= 1")

// Entry is one versioned key record. Deleted is true for a tombstone; in that
// case Value is empty.
type Entry struct {
	Key     string `json:"key"`
	Value   string `json:"value,omitempty"`
	Version int64  `json:"version"`
	Deleted bool   `json:"deleted,omitempty"`
}

// Store is safe for concurrent use.
type Store struct {
	mu       sync.RWMutex
	data     map[string]Entry
	revision int64
	epoch    int64
}

func New() *Store {
	return &Store{data: make(map[string]Entry)}
}

// Revision returns the current global revision (0 means empty/unmutated).
func (s *Store) Revision() int64 {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.revision
}

// Epoch returns the current epoch (bumped by Reset).
func (s *Store) Epoch() int64 {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.epoch
}

// Put sets key to value. If version <= 0 it is treated as a fresh write with
// the key's current version + 1. A stale explicit version (lower than the
// stored one) is ignored, matching LWW semantics; the returned bool reports
// whether anything changed.
func (s *Store) Put(key, value string, version int64) (changed bool, err error) {
	if version < 0 {
		return false, ErrInvalidVersion
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	cur, ok := s.data[key]
	if version == 0 {
		if ok {
			version = cur.Version + 1
		} else {
			version = 1
		}
	}
	if ok && version <= cur.Version {
		// Stale or equal-version write: keep the current record.
		return false, nil
	}
	s.data[key] = Entry{Key: key, Value: value, Version: version}
	s.revision++
	return true, nil
}

// Delete writes a tombstone for key at the given version (version 0 means
// current+1). Deleting a missing key is allowed: the tombstone must still
// propagate to peers that may hold the key. Returns whether state changed.
func (s *Store) Delete(key string, version int64) (changed bool, err error) {
	if version < 0 {
		return false, ErrInvalidVersion
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	cur, ok := s.data[key]
	if version == 0 {
		if ok {
			version = cur.Version + 1
		} else {
			version = 1
		}
	}
	if ok && version <= cur.Version {
		return false, nil
	}
	s.data[key] = Entry{Key: key, Version: version, Deleted: true}
	s.revision++
	return true, nil
}

// Get returns the current record for key. The second result is false when the
// key has never been seen at all (distinct from a tombstone, whose entry is
// returned with Deleted=true).
func (s *Store) Get(key string) (Entry, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	e, ok := s.data[key]
	return e, ok
}

// ApplyEntries merges a batch of remote records under LWW: a remote entry
// wins iff its version is strictly greater than the local record's version.
// Returns the entries that were actually applied (including tombstones).
func (s *Store) ApplyEntries(entries []Entry) []Entry {
	s.mu.Lock()
	defer s.mu.Unlock()
	var applied []Entry
	for _, e := range entries {
		if e.Version < 1 {
			continue
		}
		cur, ok := s.data[e.Key]
		if ok && e.Version <= cur.Version {
			continue
		}
		if !e.Deleted {
			s.data[e.Key] = Entry{Key: e.Key, Value: e.Value, Version: e.Version}
		} else {
			s.data[e.Key] = Entry{Key: e.Key, Version: e.Version, Deleted: true}
		}
		applied = append(applied, e)
		s.revision++
	}
	return applied
}

// Snapshot returns a point-in-time copy of every record (including
// tombstones) plus the revision at which it was taken. Iteration order is
// unspecified; callers that need determinism sort by key themselves.
type Snapshot struct {
	Revision int64
	Epoch    int64
	Entries  []Entry
}

func (s *Store) Snapshot() Snapshot {
	s.mu.RLock()
	defer s.mu.RUnlock()
	entries := make([]Entry, 0, len(s.data))
	for _, e := range s.data {
		entries = append(entries, e)
	}
	return Snapshot{Revision: s.revision, Epoch: s.epoch, Entries: entries}
}

// Reset empties the store and starts a new epoch (revision back to 0).
func (s *Store) Reset() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.data = make(map[string]Entry)
	s.revision = 0
	s.epoch++
}
