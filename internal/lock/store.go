// Package lock implements a single-lock lease service that issues strictly
// increasing fencing tokens.
//
// Invariants:
//   - Only one holder may own the lease at a time.
//   - Every successful Acquire (including the first after expiry) consumes a
//     fresh, strictly larger token.
//   - The token counter (nextToken) and any live lease are persisted on every
//     state change, so a restart never hands out a token smaller than one
//     issued before the restart.
package lock

import (
	"errors"
	"sync"
	"time"

	"fencingdemo/clock"
)

var (
	// ErrHeld means another client currently owns the (non-expired) lease.
	ErrHeld = errors.New("lock already held by another client")
	// ErrNotHolder means the caller does not own the lease, or the lease id is empty.
	ErrNotHolder = errors.New("lease id is empty or does not match the current holder")
	// ErrExpired means the caller owns the lease but its TTL has elapsed.
	ErrExpired = errors.New("lease has expired")
)

// Lease is the state of one lock grant.
type Lease struct {
	ID       string
	Token    int64
	IssuedAt time.Time
	Expires  time.Time
}

// snapshot is the on-disk representation. TTL is derivable from the lease
// timestamps, but persisting it lets a reconstructed store report the same
// value after a restart.
type snapshot struct {
	NextToken int64  `json:"next_token"`
	Lease     *Lease `json:"lease,omitempty"`
	TTLMillis int64  `json:"ttl_millis,omitempty"`
}

// Store is the lock state machine. All mutating operations are serialized by
// mu and immediately persisted to file.
type Store struct {
	mu        sync.Mutex
	clk       clock.Clock
	path      string
	ttl       time.Duration
	nextToken int64
	lease     *Lease
}

// NewStore opens (or creates) the store at path. If a snapshot file exists it
// is loaded; an expired lease is dropped on load but its token counter is
// kept, so restarting after expiry still produces a higher token.
func NewStore(clk clock.Clock, path string, ttl time.Duration) (*Store, error) {
	if ttl <= 0 {
		return nil, errors.New("ttl must be positive")
	}
	s := &Store{clk: clk, path: path, ttl: ttl, nextToken: 1}
	if err := s.load(); err != nil {
		return nil, err
	}
	return s, nil
}

// Acquire grants the lock to leaseID.
//
// It fails if another client still holds a live lease. If the lock is free or
// the on-disk lease has expired, the caller gets a new lease carrying the
// next fencing token. Re-acquiring with the same live leaseID is also an
// error (the correct operation is Renew).
func (s *Store) Acquire(leaseID string) (Lease, error) {
	if leaseID == "" {
		return Lease{}, errors.New("lease id must not be empty")
	}
	s.mu.Lock()
	defer s.mu.Unlock()

	now := s.clk.Now()
	if s.lease != nil && now.Before(s.lease.Expires) {
		if s.lease.ID == leaseID {
			return Lease{}, ErrHeld
		}
		return Lease{}, ErrHeld
	}

	s.lease = &Lease{
		ID:       leaseID,
		Token:    s.nextToken,
		IssuedAt: now,
		Expires:  now.Add(s.ttl),
	}
	s.nextToken++
	if err := s.persistLocked(); err != nil {
		return Lease{}, err
	}
	return *s.lease, nil
}

// Renew extends the caller's lease by one TTL from the current clock time.
// Renewing an expired lease fails with ErrExpired; a wrong/empty id fails
// with ErrNotHolder.
func (s *Store) Renew(leaseID string) (Lease, error) {
	if leaseID == "" {
		return Lease{}, ErrNotHolder
	}
	s.mu.Lock()
	defer s.mu.Unlock()

	l := s.liveLeaseLocked()
	if l == nil {
		if s.lease != nil && s.lease.ID == leaseID {
			return Lease{}, ErrExpired
		}
		return Lease{}, ErrNotHolder
	}
	if l.ID != leaseID {
		return Lease{}, ErrNotHolder
	}

	now := s.clk.Now()
	l.IssuedAt = now
	l.Expires = now.Add(s.ttl)
	if err := s.persistLocked(); err != nil {
		return Lease{}, err
	}
	return *l, nil
}

// Release frees the lock. It fails for a wrong/empty id or an expired lease
// (an expired lease is no longer the caller's property).
func (s *Store) Release(leaseID string) error {
	if leaseID == "" {
		return ErrNotHolder
	}
	s.mu.Lock()
	defer s.mu.Unlock()

	if s.lease == nil {
		return ErrNotHolder
	}
	if !s.clk.Now().Before(s.lease.Expires) {
		if s.lease.ID == leaseID {
			return ErrExpired
		}
		return ErrNotHolder
	}
	if s.lease.ID != leaseID {
		return ErrNotHolder
	}

	s.lease = nil
	return s.persistLocked()
}

// Status reports the current lock state. held is false when no live lease exists.
func (s *Store) Status() (lease *Lease, ttl time.Duration, held bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	l := s.liveLeaseLocked()
	if l == nil {
		return nil, s.ttl, false
	}
	cp := *l
	return &cp, s.ttl, true
}

// NextToken returns the token the next successful Acquire will hand out.
// It is primarily used to make restart-monotonicity assertions.
func (s *Store) NextToken() int64 {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.nextToken
}

func (s *Store) liveLeaseLocked() *Lease {
	if s.lease == nil {
		return nil
	}
	if !s.clk.Now().Before(s.lease.Expires) {
		return nil
	}
	return s.lease
}
