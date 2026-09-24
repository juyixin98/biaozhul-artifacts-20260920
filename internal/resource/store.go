// Package resource implements a storage service that uses fencing tokens to
// reject stale writers.
//
// Every write must carry the token its writer received from the lock
// service. The resource keeps a high-water mark: a write is accepted only if
// its token is greater than or equal to the largest token already observed.
// The high-water mark and current value are persisted, so after a restart a
// delayed old-holder write is still refused.
package resource

import (
	"errors"
	"fmt"
	"sync"
)

var (
	// ErrBadToken means the token is missing/non-positive.
	ErrBadToken = errors.New("fencing token must be a positive integer")
	// ErrStaleWrite means the token is below the high-water mark.
	ErrStaleWrite = errors.New("stale write rejected: token is behind the resource high-water mark")
)

// Store is the resource being protected.
type Store struct {
	mu        sync.Mutex
	path      string
	value     string
	version   int64
	lastToken int64
}

// NewStore opens (or creates) the resource store at path.
func NewStore(path string) (*Store, error) {
	s := &Store{path: path}
	if err := s.load(); err != nil {
		return nil, err
	}
	return s, nil
}

// Write applies a client write. It accepts token >= lastToken. Note that with
// a correctly functioning lock two different holders never share a token;
// equality is accepted to make the resource's rule literally monotonic and
// to allow a legitimate holder to retry the same write.
func (s *Store) Write(token int64, value string) (version int64, appliedToken int64, err error) {
	if token < 1 {
		return 0, 0, ErrBadToken
	}
	s.mu.Lock()
	defer s.mu.Unlock()

	if token < s.lastToken {
		return 0, s.lastToken, ErrStaleWrite
	}

	s.lastToken = token
	s.value = value
	s.version++
	if err := s.persistLocked(); err != nil {
		return 0, s.lastToken, fmt.Errorf("persist resource: %w", err)
	}
	return s.version, s.lastToken, nil
}

// Read returns the current value, its version, and the high-water mark.
func (s *Store) Read() (value string, version int64, highWater int64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.value, s.version, s.lastToken
}
