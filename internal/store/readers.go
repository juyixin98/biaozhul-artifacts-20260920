package store

import (
	"sync"
	"time"
)

// readerRegistry tracks active historical readers. Each reader holds a lease
// pinning the base snapshot height it reads from. Pruning consults the
// registry and never deletes data an unexpired lease depends on. A lease
// that outlives its deadline is no longer protected — and the reader itself
// fails explicitly with ErrReaderTimeout on its next deadline check.
type readerRegistry struct {
	mu     sync.Mutex
	now    func() time.Time
	next   uint64
	leases map[uint64]readerLease
}

type readerLease struct {
	base     uint64
	deadline time.Time
}

func newReaderRegistry(now func() time.Time) *readerRegistry {
	return &readerRegistry{now: now, leases: map[uint64]readerLease{}}
}

func (r *readerRegistry) acquire(base uint64, deadline time.Time) uint64 {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.next++
	id := r.next
	r.leases[id] = readerLease{base: base, deadline: deadline}
	return id
}

func (r *readerRegistry) release(id uint64) {
	r.mu.Lock()
	defer r.mu.Unlock()
	delete(r.leases, id)
}

// minActiveBase returns the smallest base snapshot height among unexpired
// leases. Deltas at or below it and the snapshot at it must survive pruning.
func (r *readerRegistry) minActiveBase(now time.Time) (uint64, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	var min uint64
	found := false
	for _, l := range r.leases {
		if now.After(l.deadline) {
			continue
		}
		if !found || l.base < min {
			min = l.base
			found = true
		}
	}
	return min, found
}

// hasActiveBase reports whether any unexpired lease pins exactly base.
func (r *readerRegistry) hasActiveBase(base uint64, now time.Time) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	for _, l := range r.leases {
		if l.base == base && !now.After(l.deadline) {
			return true
		}
	}
	return false
}
