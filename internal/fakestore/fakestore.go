// Package fakestore is the in-process stand-in for the external object
// storage dependency. It never touches the network or disk; latency and
// failures can be injected so tests exercise error paths deterministically.
package fakestore

import (
	"context"
	"errors"
	"sync"
	"time"

	"tenantiso/internal/clock"
)

// ErrInjected is returned when an injected failure is consumed.
var ErrInjected = errors.New("fakestore: injected failure")

// Store is a fake external KV service backed by process memory.
type Store struct {
	clk      clock.Clock
	mu       sync.Mutex
	data     map[string][]byte
	latency  time.Duration
	failNext int
	puts     int
	gets     int
}

// New creates a Store using clk for simulated latency.
func New(clk clock.Clock) *Store {
	return &Store{clk: clk, data: make(map[string][]byte)}
}

// SetLatency makes every operation take at least d (via the injected clock).
func (s *Store) SetLatency(d time.Duration) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.latency = d
}

// InjectFailures makes the next n operations fail with ErrInjected.
func (s *Store) InjectFailures(n int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.failNext += n
}

// Stats reports call counts, for assertions in tests.
func (s *Store) Stats() (puts, gets int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.puts, s.gets
}

func (s *Store) beforeCall(ctx context.Context) error {
	s.mu.Lock()
	latency := s.latency
	if s.failNext > 0 {
		s.failNext--
		s.mu.Unlock()
		return ErrInjected
	}
	s.mu.Unlock()
	if latency > 0 {
		select {
		case <-s.clk.After(latency):
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	return nil
}

// Put stores val under key.
func (s *Store) Put(ctx context.Context, key string, val []byte) error {
	if err := s.beforeCall(ctx); err != nil {
		return err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.puts++
	cp := make([]byte, len(val))
	copy(cp, val)
	s.data[key] = cp
	return nil
}

// Get fetches the value stored under key.
func (s *Store) Get(ctx context.Context, key string) ([]byte, bool, error) {
	if err := s.beforeCall(ctx); err != nil {
		return nil, false, err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.gets++
	val, ok := s.data[key]
	if !ok {
		return nil, false, nil
	}
	cp := make([]byte, len(val))
	copy(cp, val)
	return cp, true, nil
}
