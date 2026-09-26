// Package store is the concurrency-safe resource store.
//
// Every state change is a compare-and-swap performed under a single mutex:
// the precondition check, the (fake) downstream side effect and the
// version/content commit happen in one critical section, so a conditional
// request can never observe a version and commit against a stale one, and a
// failed side effect leaves no mutation behind. Version numbers are
// monotonically increasing and are NEVER reused, even when a resource is
// deleted and later recreated.
package store

import (
	"errors"
	"sync"
	"time"

	"etagrace/internal/clock"
	"etagrace/internal/etag"
)

var (
	// ErrPreconditionFailed means If-Match did not match the current state.
	ErrPreconditionFailed = errors.New("precondition failed")
	// ErrNotExisting means If-Match was supplied for a resource that has no
	// current representation (unknown, or deleted).
	ErrNotExisting = errors.New("resource has no current representation")
	// ErrAlreadyExists is returned by Create when the key is live.
	ErrAlreadyExists = errors.New("resource already exists")
)

// BeforeCommitHook runs after the precondition matches but before the new
// version is committed, inside the same critical section. Returning an error
// aborts the whole operation without any state change. The version argument
// is the version the resource WILL have if the hook succeeds.
type BeforeCommitHook func(version int64) error

// Snapshot is an immutable point-in-time view of a resource.
type Snapshot struct {
	Key       string
	Version   int64
	Content   []byte
	Tag       etag.ETag
	UpdatedAt time.Time
	Deleted   bool
}

type resource struct {
	version   int64 // last assigned version; retained on the tombstone
	content   []byte
	tag       etag.ETag
	updatedAt time.Time
	deleted   bool
}

// Store holds resources keyed by name.
type Store struct {
	mu   sync.Mutex
	data map[string]*resource
	clk  clock.Clock
}

func New(clk clock.Clock) *Store {
	return &Store{data: make(map[string]*resource), clk: clk}
}

// Get returns the live snapshot. A deleted or unknown resource reports ok=false.
func (s *Store) Get(key string) (Snapshot, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	r, ok := s.data[key]
	if !ok || r.deleted {
		return Snapshot{}, false
	}
	return liveSnapshotLocked(key, r), true
}

// Create inserts a new resource. If a tombstone exists (the key was deleted),
// the resource is recreated with a brand-new, strictly higher version.
func (s *Store) Create(key string, content []byte) (Snapshot, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if r, ok := s.data[key]; ok && !r.deleted {
		return Snapshot{}, ErrAlreadyExists
	}
	snap := s.commitLocked(key, content)
	return snap, nil
}

// MatchResult classifies a conditional request outcome.
type MatchResult struct {
	Committed bool
	Snapshot  Snapshot // current snapshot after the attempt (untouched on failure)
	// Reason is empty on success; "not_existing", "precondition_failed" or
	// "hook_failed".
	Reason string
	// HookErr carries the BeforeCommitHook failure when Reason == "hook_failed".
	HookErr error
}

// ConditionalPut applies content only when the If-Match precondition holds,
// atomically. Explicit tags use STRONG comparison only. When beforeCommit is
// non-nil it runs before the commit; its error aborts the operation with zero
// mutation.
func (s *Store) ConditionalPut(key string, tags []etag.ETag, matchAny bool, content []byte, beforeCommit BeforeCommitHook) MatchResult {
	s.mu.Lock()
	defer s.mu.Unlock()

	r, ok := s.data[key]
	if !ok || r.deleted {
		return MatchResult{Reason: "not_existing"}
	}
	if !matches(r.tag, tags, matchAny) {
		return MatchResult{Reason: "precondition_failed", Snapshot: liveSnapshotLocked(key, r)}
	}
	if beforeCommit != nil {
		if err := beforeCommit(r.version + 1); err != nil {
			return MatchResult{Reason: "hook_failed", Snapshot: liveSnapshotLocked(key, r), HookErr: err}
		}
	}
	snap := s.commitLocked(key, content)
	return MatchResult{Committed: true, Snapshot: snap}
}

// ConditionalDelete tombstones the resource only when the precondition holds.
// The version counter still advances, so a recreated resource cannot reuse the
// deleted representation's version or ETag.
func (s *Store) ConditionalDelete(key string, tags []etag.ETag, matchAny bool, beforeCommit BeforeCommitHook) MatchResult {
	s.mu.Lock()
	defer s.mu.Unlock()

	r, ok := s.data[key]
	if !ok || r.deleted {
		return MatchResult{Reason: "not_existing"}
	}
	if !matches(r.tag, tags, matchAny) {
		return MatchResult{Reason: "precondition_failed", Snapshot: liveSnapshotLocked(key, r)}
	}
	if beforeCommit != nil {
		if err := beforeCommit(r.version + 1); err != nil {
			return MatchResult{Reason: "hook_failed", Snapshot: liveSnapshotLocked(key, r), HookErr: err}
		}
	}
	r.version++
	r.content = nil
	r.tag = etag.ETag{}
	r.updatedAt = s.clk.Now()
	r.deleted = true
	return MatchResult{Committed: true, Snapshot: Snapshot{Key: key, Version: r.version, Deleted: true, UpdatedAt: r.updatedAt}}
}

// commitLocked assigns the next version and a content-bound strong ETag.
// Caller must hold s.mu.
func (s *Store) commitLocked(key string, content []byte) Snapshot {
	r := s.data[key]
	if r == nil {
		r = &resource{}
		s.data[key] = r
	}
	content = append([]byte(nil), content...)
	r.version++
	r.content = content
	r.tag = etag.New(r.version, content)
	r.updatedAt = s.clk.Now()
	r.deleted = false
	return liveSnapshotLocked(key, r)
}

func liveSnapshotLocked(key string, r *resource) Snapshot {
	return Snapshot{
		Key:       key,
		Version:   r.version,
		Content:   append([]byte(nil), r.content...),
		Tag:       r.tag,
		UpdatedAt: r.updatedAt,
	}
}

// matches is the strong comparison used by state-changing requests:
//   - "*" matches exactly when a current representation exists;
//   - an explicit tag matches only when BOTH tags are strong and equal.
func matches(current etag.ETag, supplied []etag.ETag, matchAny bool) bool {
	if matchAny {
		return true // existence was already checked by the caller
	}
	for _, t := range supplied {
		if etag.StrongEqual(current, t) {
			return true
		}
	}
	return false
}
