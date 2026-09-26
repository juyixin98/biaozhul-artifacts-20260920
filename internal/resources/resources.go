// Package resources manages closeable dependencies and closes them in strict
// LIFO (reverse registration) order, recording the close order for reporting.
package resources

import (
	"context"
	"fmt"
	"sync"
	"time"
)

// Resource is a named closeable dependency (fake DB, fake queue, ...).
type Resource interface {
	Name() string
	Close(ctx context.Context) error
}

// CloseRecord is one entry in the recorded close order.
type CloseRecord struct {
	Order    int       `json:"order"`
	Name     string    `json:"name"`
	ClosedAt time.Time `json:"closed_at"`
	Err      string    `json:"err,omitempty"`
}

// Registry holds resources in registration order.
type Registry struct {
	mu       sync.Mutex
	items    []Resource
	closed   bool
	closeLog []CloseRecord
}

func NewRegistry() *Registry { return &Registry{} }

// Register adds a resource. Resources are closed in reverse registration
// order (LIFO): the most recently registered dependency closes first.
func (r *Registry) Register(res Resource) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.items = append(r.items, res)
}

// CloseAll closes every registered resource in LIFO order and records the
// order. It is idempotent; later calls are no-ops.
func (r *Registry) CloseAll(ctx context.Context, now func() time.Time) []CloseRecord {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.closed {
		out := make([]CloseRecord, len(r.closeLog))
		copy(out, r.closeLog)
		return out
	}
	r.closed = true
	for i := len(r.items) - 1; i >= 0; i-- {
		res := r.items[i]
		rec := CloseRecord{Order: len(r.closeLog) + 1, Name: res.Name(), ClosedAt: now()}
		if err := res.Close(ctx); err != nil {
			rec.Err = fmt.Sprintf("%v", err)
		}
		r.closeLog = append(r.closeLog, rec)
	}
	out := make([]CloseRecord, len(r.closeLog))
	copy(out, r.closeLog)
	return out
}

// CloseLog returns the recorded close order (empty before CloseAll).
func (r *Registry) CloseLog() []CloseRecord {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]CloseRecord, len(r.closeLog))
	copy(out, r.closeLog)
	return out
}
