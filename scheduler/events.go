package scheduler

import (
	"sync"
)

// EventStore is an append-only log of state-change events.
// Implementations must be safe for concurrent use.
type EventStore interface {
	Append(ev Event)
	// Since returns events with EventID strictly greater than afterID,
	// at most limit entries (limit <= 0 means no limit).
	Since(afterID int64, limit int) []Event
	// All returns every recorded event in EventID order.
	All() []Event
}

// MemoryStore keeps all events in memory, guarded by a mutex.
type MemoryStore struct {
	mu     sync.RWMutex
	events []Event
}

// NewMemoryStore creates an empty in-memory event store.
func NewMemoryStore() *MemoryStore {
	return &MemoryStore{}
}

func (s *MemoryStore) Append(ev Event) {
	s.mu.Lock()
	defer s.mu.Unlock()
	ev.EventID = int64(len(s.events)) + 1
	s.events = append(s.events, ev)
}

func (s *MemoryStore) Since(afterID int64, limit int) []Event {
	s.mu.RLock()
	defer s.mu.RUnlock()
	start := afterID
	if start < 0 {
		start = 0
	}
	if start > int64(len(s.events)) {
		start = int64(len(s.events))
	}
	out := append([]Event(nil), s.events[start:]...)
	if limit > 0 && len(out) > limit {
		out = out[:limit]
	}
	return out
}

func (s *MemoryStore) All() []Event {
	return s.Since(0, 0)
}
