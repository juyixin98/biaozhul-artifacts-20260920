// Package store implements the in-memory resource store with atomic
// compare-and-swap semantics: a resource's version and content always
// change together, and a failed update leaves no side effects.
package store

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"sync"
	"time"

	"conditionupdate/internal/clock"
	"conditionupdate/internal/etag"
)

var (
	// ErrNotFound means the resource does not exist (and no tombstone applies).
	ErrNotFound = errors.New("resource not found")
	// ErrPreconditionFailed means If-Match / If-None-Match evaluated to false.
	ErrPreconditionFailed = errors.New("precondition failed")
	// ErrPreconditionRequired means the request carried no precondition at all.
	ErrPreconditionRequired = errors.New("precondition required")
)

// Resource is one stored value.
type Resource struct {
	ID        string    `json:"id"`
	Version   uint64    `json:"version"`
	ETag      string    `json:"etag"` // always a strong tag, e.g. "doc-v3-a1b2c3d4"
	Data      []byte    `json:"data"`
	CreatedAt time.Time `json:"createdAt"`
	UpdatedAt time.Time `json:"updatedAt"`
}

// Update describes a conditional write.
type Update struct {
	// IfMatch, when non-nil, is the parsed If-Match header.
	IfMatch *etag.Condition
	// IfNoneMatch, when non-nil, is the parsed If-None-Match header.
	IfNoneMatch *etag.Condition
	// Data is the new content.
	Data []byte
}

// BeforeCommit is invoked while the store lock is held, after the
// precondition passed and the new state is computed but before it is
// installed. Returning an error aborts the update with zero side effects.
type BeforeCommit func(next Resource) error

// Store is a concurrency-safe in-memory resource map.
type Store struct {
	mu    sync.Mutex
	clock clock.Clock
	items map[string]*Resource
}

// New returns an empty Store using the given clock.
func New(c clock.Clock) *Store {
	return &Store{clock: c, items: make(map[string]*Resource)}
}

// Get returns a copy of the resource, or ErrNotFound.
func (s *Store) Get(id string) (Resource, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	r, ok := s.items[id]
	if !ok {
		return Resource{}, ErrNotFound
	}
	return *r, nil
}

// Put applies a conditional create-or-replace. Exactly one of the
// preconditions must be present:
//
//   - If-Match: update an existing resource whose current ETag matches
//     under strong comparison (`*` matches any existing resource).
//   - If-None-Match: * — create only if the resource does not exist.
//
// A request with neither is rejected with ErrPreconditionRequired.
// On success version and content change atomically; on any error the
// store is untouched.
func (s *Store) Put(id string, u Update, hook BeforeCommit) (Resource, error) {
	if u.IfMatch == nil && u.IfNoneMatch == nil {
		return Resource{}, ErrPreconditionRequired
	}

	s.mu.Lock()
	defer s.mu.Unlock()

	cur, exists := s.items[id]
	var curTag etag.ETag
	if exists {
		curTag = etag.ETag{Value: cur.ETag}
	}

	switch {
	case u.IfMatch != nil:
		if !exists {
			return Resource{}, fmt.Errorf("%w: %s", ErrPreconditionFailed, "If-Match on missing resource")
		}
		if !u.IfMatch.StronglyMatchesAny(curTag, true) {
			return Resource{}, fmt.Errorf("%w: %s", ErrPreconditionFailed, "If-Match mismatch")
		}
	case u.IfNoneMatch != nil:
		// If-None-Match uses the weak comparison function: `*` fails
		// when the resource exists; a listed tag fails when its opaque
		// value equals the current ETag, even if either tag is weak.
		if u.IfNoneMatch.WeaklyMatchesAny(curTag, exists) {
			return Resource{}, fmt.Errorf("%w: %s", ErrPreconditionFailed, "If-None-Match matched current state")
		}
	}

	now := s.clock.Now()
	next := Resource{
		ID:        id,
		Version:   1,
		Data:      append([]byte(nil), u.Data...),
		CreatedAt: now,
		UpdatedAt: now,
	}
	if exists {
		next.Version = cur.Version + 1
		next.CreatedAt = cur.CreatedAt
	}
	next.ETag = makeETag(id, next.Version, next.Data)

	if hook != nil {
		if err := hook(next); err != nil {
			// Abort: nothing was installed, the store is unchanged.
			return Resource{}, err
		}
	}

	s.items[id] = &next
	return next, nil
}

// Delete removes the resource if the If-Match precondition holds.
// A missing precondition is rejected with ErrPreconditionRequired.
// Deleting a missing resource yields ErrNotFound.
func (s *Store) Delete(id string, ifMatch *etag.Condition) error {
	if ifMatch == nil {
		return ErrPreconditionRequired
	}

	s.mu.Lock()
	defer s.mu.Unlock()

	cur, ok := s.items[id]
	var curTag etag.ETag
	if ok {
		curTag = etag.ETag{Value: cur.ETag}
	}
	// Evaluate the precondition before existence (RFC 7232 §3.1):
	// deleting a missing resource with a failing If-Match is a 412,
	// not a 404.
	if !ifMatch.StronglyMatchesAny(curTag, ok) {
		return fmt.Errorf("%w: %s", ErrPreconditionFailed, "If-Match mismatch")
	}
	delete(s.items, id)
	return nil
}

// Snapshot returns copies of all resources, for admin/test inspection.
func (s *Store) Snapshot() []Resource {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]Resource, 0, len(s.items))
	for _, r := range s.items {
		out = append(out, *r)
	}
	return out
}

// makeETag builds a strong entity tag binding id, version and content.
func makeETag(id string, version uint64, data []byte) string {
	sum := sha256.Sum256(data)
	return fmt.Sprintf("%s-v%d-%s", id, version, hex.EncodeToString(sum[:4]))
}
