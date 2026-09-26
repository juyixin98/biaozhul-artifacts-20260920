// Package ledger records the structured outcome of every accepted request so
// the final shutdown report can prove "accepted requests either completed or
// were explicitly cancelled".
package ledger

import (
	"sync"
	"sync/atomic"
	"time"
)

// Outcome is the terminal state of an accepted request.
type Outcome string

const (
	OutcomeCompleted Outcome = "completed"
	OutcomeCancelled Outcome = "cancelled"
)

// Entry is one accepted request's lifecycle record.
type Entry struct {
	ID        uint64    `json:"id"`
	Path      string    `json:"path"`
	Kind      string    `json:"kind"` // "request" or "background-task"
	StartedAt time.Time `json:"started_at"`
	EndedAt   time.Time `json:"ended_at"`
	Outcome   Outcome   `json:"outcome"`
	Detail    string    `json:"detail,omitempty"`
}

// Ledger is a goroutine-safe append-only record of request outcomes.
type Ledger struct {
	mu      sync.Mutex
	entries []Entry
	nextID  atomic.Uint64
}

func New() *Ledger { return &Ledger{} }

// Begin registers a new accepted unit of work and returns its id.
func (l *Ledger) Begin(path, kind string, startedAt time.Time) uint64 {
	id := l.nextID.Add(1)
	l.mu.Lock()
	defer l.mu.Unlock()
	l.entries = append(l.entries, Entry{ID: id, Path: path, Kind: kind, StartedAt: startedAt})
	return id
}

// End marks an entry finished with its terminal outcome.
func (l *Ledger) End(id uint64, endedAt time.Time, outcome Outcome, detail string) {
	l.mu.Lock()
	defer l.mu.Unlock()
	for i := range l.entries {
		if l.entries[i].ID == id {
			l.entries[i].EndedAt = endedAt
			l.entries[i].Outcome = outcome
			l.entries[i].Detail = detail
			return
		}
	}
}

// Snapshot returns a copy of all entries.
func (l *Ledger) Snapshot() []Entry {
	l.mu.Lock()
	defer l.mu.Unlock()
	out := make([]Entry, len(l.entries))
	copy(out, l.entries)
	return out
}
