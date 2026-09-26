// Package blobstore is an in-process stand-in for an external object store.
//
// Nothing in this project talks to a real production system. The memory
// store is the fake service; FlakyStore decorates it to inject transient
// failures and latency for loader/retry tests.
package blobstore

import (
	"context"
	"errors"
	"sync"
	"sync/atomic"
	"time"

	"github.com/example/rangeserver/internal/clock"
)

var (
	// ErrNotFound is returned when a key is absent.
	ErrNotFound = errors.New("blobstore: key not found")
	// ErrUnavailable is injected by FlakyStore.
	ErrUnavailable = errors.New("blobstore: service unavailable")
)

// Store is the blob storage boundary. Returned byte slices must be treated
// as immutable by callers; implementations hand out defensive copies.
type Store interface {
	Get(ctx context.Context, key string) ([]byte, error)
	Put(ctx context.Context, key string, data []byte) error
	List() []string
}

// MemoryStore is the deterministic, in-process fake service.
type MemoryStore struct {
	mu    sync.RWMutex
	blobs map[string][]byte
}

// NewMemoryStore returns an empty memory store.
func NewMemoryStore() *MemoryStore {
	return &MemoryStore{blobs: make(map[string][]byte)}
}

func (s *MemoryStore) Get(_ context.Context, key string) ([]byte, error) {
	s.mu.RLock()
	data, ok := s.blobs[key]
	s.mu.RUnlock()
	if !ok {
		return nil, ErrNotFound
	}
	return append([]byte(nil), data...), nil
}

func (s *MemoryStore) Put(_ context.Context, key string, data []byte) error {
	s.mu.Lock()
	s.blobs[key] = append([]byte(nil), data...)
	s.mu.Unlock()
	return nil
}

func (s *MemoryStore) List() []string {
	s.mu.RLock()
	keys := make([]string, 0, len(s.blobs))
	for k := range s.blobs {
		keys = append(keys, k)
	}
	s.mu.RUnlock()
	return keys
}

// FlakyStore wraps a Store and fails the first Failures Get calls,
// optionally adding latency through the injected Clock.
type FlakyStore struct {
	Inner    Store
	Clock    clock.Clock
	Failures int32 // number of Get calls that still fail
	Latency  time.Duration

	failed int32
}

func (f *FlakyStore) Get(ctx context.Context, key string) ([]byte, error) {
	if f.Latency > 0 && f.Clock != nil {
		if err := f.Clock.Sleep(ctx, f.Latency); err != nil {
			return nil, err
		}
	}
	if atomic.LoadInt32(&f.failed) < atomic.LoadInt32(&f.Failures) {
		atomic.AddInt32(&f.failed, 1)
		return nil, ErrUnavailable
	}
	return f.Inner.Get(ctx, key)
}

func (f *FlakyStore) Put(ctx context.Context, key string, data []byte) error {
	return f.Inner.Put(ctx, key, data)
}

func (f *FlakyStore) List() []string { return f.Inner.List() }
