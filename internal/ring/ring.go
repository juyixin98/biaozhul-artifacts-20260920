// Package ring is a fixed-capacity, in-memory FIFO store of ingested events.
// It backs the query API and bounds memory: when full, the oldest event is
// overwritten.
package ring

import (
	"sync"
	"time"
)

// Event is one ingested log line with its assignment metadata.
type Event struct {
	Seq       int64     `json:"seq"`
	Time      time.Time `json:"time"`
	Line      string    `json:"line"`
	ClusterID int       `json:"cluster_id"`
	Template  string    `json:"template"`
	Version   int       `json:"version"`
}

// Ring is a thread-safe bounded FIFO.
type Ring struct {
	mu      sync.RWMutex
	events  []Event
	next    int // write head
	size    int // number of valid entries
	cap     int
	lastSeq int64
}

// New creates a ring with the given capacity.
func New(capacity int) *Ring {
	if capacity < 1 {
		capacity = 1
	}
	return &Ring{events: make([]Event, capacity), cap: capacity}
}

// Add appends an event, evicting the oldest when full. The event's Seq is
// assigned from the ring's monotonic counter and returned.
func (r *Ring) Add(e Event) Event {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.lastSeq++
	e.Seq = r.lastSeq
	r.events[r.next] = e
	r.next = (r.next + 1) % r.cap
	if r.size < r.cap {
		r.size++
	}
	return e
}

// Restore replaces the ring contents with ordered events (oldest-first).
// When the input exceeds capacity only the newest entries survive, and the
// sequence counter continues above the highest restored Seq.
func (r *Ring) Restore(ordered []Event) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.events = make([]Event, r.cap)
	r.next = 0
	r.size = 0
	r.lastSeq = 0
	for _, e := range ordered {
		if e.Seq > r.lastSeq {
			r.lastSeq = e.Seq
		}
		if r.size < r.cap {
			r.events[r.next] = e
			r.next = (r.next + 1) % r.cap
			r.size++
		} else {
			// Full: shift out the oldest (events are presented oldest-first).
			copy(r.events, r.events[1:])
			r.events[r.cap-1] = e
			r.next = 0
		}
	}
	if r.size < r.cap {
		r.next = r.size % r.cap
	}
}

// Snapshot returns events in ingestion (oldest-first) order.
func (r *Ring) Snapshot() []Event {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return r.appendOrdered(nil)
}

// Query returns up to limit events matching the optional cluster ID and
// substring filter, in newest-first order. limit <= 0 means a default cap.
func (r *Ring) Query(clusterID int, hasCluster bool, substring string, limit int) []Event {
	const defaultLimit = 100
	if limit <= 0 || limit > 1000 {
		limit = defaultLimit
	}
	r.mu.RLock()
	defer r.mu.RUnlock()

	ordered := r.appendOrdered(nil)
	out := make([]Event, 0, limit)
	for i := len(ordered) - 1; i >= 0; i-- {
		e := ordered[i]
		if hasCluster && e.ClusterID != clusterID {
			continue
		}
		if substring != "" && !contains(e.Line, substring) {
			continue
		}
		out = append(out, e)
		if len(out) >= limit {
			break
		}
	}
	return out
}

func (r *Ring) appendOrdered(dst []Event) []Event {
	if r.size == 0 {
		return dst
	}
	start := (r.next - r.size + r.cap) % r.cap
	for k := 0; k < r.size; k++ {
		dst = append(dst, r.events[(start+k)%r.cap])
	}
	return dst
}

// Stats reports capacity usage and the highest assigned sequence number.
func (r *Ring) Stats() (stored, capacity int, lastSeq int64) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return r.size, r.cap, r.lastSeq
}

func contains(s, sub string) bool {
	if len(sub) == 0 {
		return true
	}
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return true
		}
	}
	return false
}
