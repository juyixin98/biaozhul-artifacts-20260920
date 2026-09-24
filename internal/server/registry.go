package server

import (
	"context"
	"sync"
	"sync/atomic"
	"time"

	"github.com/example/compgw/internal/mapping"
	"github.com/example/compgw/internal/store"
)

// MappingRegistry holds the current active mapping snapshot per direction and
// reloads from PostgreSQL on demand. Unary calls use the latest snapshot; a
// stream pins its snapshot when the stream opens, so an in-flight stream is
// never mid-converted by a hot switch, while new streams immediately use the
// new mapping.
type MappingRegistry struct {
	store *store.Store

	mu       sync.RWMutex
	snap     map[string]*store.ActiveMapping // direction -> snapshot
	revision atomic.Uint64                   // increments on every reload (observable in tests)

	pollInterval time.Duration
}

func NewMappingRegistry(s *store.Store) *MappingRegistry {
	return &MappingRegistry{
		store:        s,
		snap:         map[string]*store.ActiveMapping{},
		pollInterval: 2 * time.Second,
	}
}

// SetPollInterval adjusts the polling fallback used by Watch (tests only).
func (r *MappingRegistry) SetPollInterval(d time.Duration) { r.pollInterval = d }

// Load fetches both active mappings from the database once.
func (r *MappingRegistry) Load(ctx context.Context) error {
	return r.Reload(ctx)
}

// Reload rereads active mappings from PostgreSQL.
func (r *MappingRegistry) Reload(ctx context.Context) error {
	next := make(map[string]*store.ActiveMapping, 2)
	for _, dir := range []string{mapping.DirectionV1ToV2, mapping.DirectionV2ToV1} {
		am, err := r.store.Active(ctx, dir)
		if err != nil {
			return err
		}
		next[dir] = am
	}
	r.mu.Lock()
	r.snap = next
	r.mu.Unlock()
	r.revision.Add(1)
	return nil
}

// Revision reports how many successful reloads happened.
func (r *MappingRegistry) Revision() uint64 { return r.revision.Load() }

// Current returns the pinned snapshot for a direction.
func (r *MappingRegistry) Current(direction string) *store.ActiveMapping {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return r.snap[direction]
}

// Watch reloads on PostgreSQL NOTIFY and on a polling fallback (e.g. missing
// LISTEN in a restricted setup), until ctx is canceled.
func (r *MappingRegistry) Watch(ctx context.Context) {
	notify, err := r.store.ListenMappings(ctx)
	if err != nil {
		notify = nil
	}
	ticker := time.NewTicker(r.pollInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-notify:
			_ = r.Reload(ctx)
		case <-ticker.C:
			_ = r.Reload(ctx)
		}
	}
}

// Pin is the immutable mapping snapshot a stream works against.
type Pin struct {
	Direction string
	Active    *store.ActiveMapping
}

// PinDirection returns the snapshot to use for a newly opened call/stream.
func (r *MappingRegistry) PinDirection(direction string) Pin {
	return Pin{Direction: direction, Active: r.Current(direction)}
}
