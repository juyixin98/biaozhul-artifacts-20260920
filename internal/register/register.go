// Package register implements a multi-replica versioned key-value register.
//
// Each key holds the set of maximal (concurrent) versions. Versions carry a
// vector clock: when a new version causally dominates an existing one, the old
// version is pruned; concurrent versions are kept side by side as siblings and
// are never silently discarded.
//
// A client merge write must carry the complete set of sibling versions it is
// replacing (the read-context). Writes with a stale context -- a context that
// fails to cover the current siblings -- are rejected, so a client acting on an
// old read cannot overwrite a concurrent version.
package register

import (
	"errors"
	"fmt"
	"sort"

	"vccsim/internal/clock"
)

// Version is one stored value with the vector clock describing its causal past.
type Version struct {
	// ID is a globally unique version identifier ("node:counter").
	ID string `json:"id"`
	// Value is the payload; arbitrary JSON captured verbatim.
	Value []byte `json:"value"`
	// VC is the version's vector clock.
	VC clock.VectorClock `json:"vc"`
}

// Sentinel errors returned by MergeWrite.
var (
	// ErrUnknownContext is returned when the request names a version id that
	// is not present among the current siblings and has never been provided.
	ErrUnknownContext = errors.New("context references unknown version id")
	// ErrStaleContext is returned when a supplied context entry is an ancestor
	// of the current siblings without covering them: the client read a state
	// that was already superseded and is trying to write on top of it.
	ErrStaleContext = errors.New("stale context: read state was superseded")
	// ErrConcurrentNotCovered is returned when current concurrent siblings are
	// not included in the supplied context. The client must read all siblings
	// and merge their values before writing.
	ErrConcurrentNotCovered = errors.New("context does not cover concurrent siblings")
)

// ContextEntry references one version the client read and intends to replace.
// VC may be omitted only when the simulator can resolve id from history.
type ContextEntry struct {
	ID string            `json:"id"`
	VC clock.VectorClock `json:"vc,omitempty"`
}

// MergeRequest is a client merge write against a key.
type MergeRequest struct {
	Node    string         // node performing the write
	Key     string         // target key
	Value   []byte         // merged payload
	Context []ContextEntry // complete sibling set the client observed
}

// MergeError reports why a merge write was rejected.
type MergeError struct {
	Err     error
	Missing []string // sibling ids missing from the context (ErrConcurrentNotCovered)
	EntryID string   // offending context entry id (ErrUnknownContext / ErrStaleContext)
}

func (e *MergeError) Error() string {
	switch {
	case errors.Is(e.Err, ErrConcurrentNotCovered):
		return fmt.Sprintf("%s: %v", ErrConcurrentNotCovered, e.Missing)
	default:
		return e.Err.Error()
	}
}

func (e *MergeError) Unwrap() error { return e.Err }

// Register holds one node's replica state for all keys.
type Register struct {
	node     string
	vc       clock.VectorClock
	siblings map[string][]Version // key -> maximal versions, sorted by ID
}

// New creates a register for node starting from the zero vector clock.
func New(node string) *Register {
	return &Register{node: node, vc: clock.VectorClock{}, siblings: map[string][]Version{}}
}

// Clock returns a copy of the node's current clock.
func (r *Register) Clock() clock.VectorClock { return clock.Copy(r.vc) }

// Snapshot returns the current maximal versions of key (nil if never written).
func (r *Register) Snapshot(key string) []Version {
	src := r.siblings[key]
	if len(src) == 0 {
		return nil
	}
	out := make([]Version, len(src))
	copy(out, src)
	return out
}

// Keys returns the sorted list of keys this register has versions for.
func (r *Register) Keys() []string {
	out := make([]string, 0, len(r.siblings))
	for k, vs := range r.siblings {
		if len(vs) > 0 {
			out = append(out, k)
		}
	}
	sort.Strings(out)
	return out
}

// LocalWrite performs a blind causal write: tick the node clock and append the
// new version. Used for offline writes and for accepted client merges.
func (r *Register) LocalWrite(key string, value []byte) Version {
	r.vc = clock.Tick(r.vc, r.node)
	v := Version{ID: fmt.Sprintf("%s:%d", r.node, r.vc[r.node]), Value: append([]byte(nil), value...), VC: clock.Copy(r.vc)}
	r.appendVersion(key, v)
	return v
}

// Ingest receives a version from another node. It merges the carried clock into
// the node clock and keeps the version unless it is causally dominated by a
// version already stored. Duplicate delivery of an identical id is a no-op.
func (r *Register) Ingest(key string, v Version) (added bool) {
	clock.MergeInto(r.vc, v.VC)
	for _, existing := range r.siblings[key] {
		if existing.ID == v.ID {
			return false // duplicate delivery: idempotent
		}
		if clock.Descends(existing.VC, v.VC) {
			return false // already know a descendant: old version, drop
		}
	}
	r.appendVersion(key, v)
	return true
}

// appendVersion inserts v and prunes every sibling dominated by v.
func (r *Register) appendVersion(key string, v Version) {
	kept := make([]Version, 0, len(r.siblings[key])+1)
	for _, s := range r.siblings[key] {
		if clock.Descends(v.VC, s.VC) {
			continue // v causally supersedes s
		}
		kept = append(kept, s)
	}
	kept = append(kept, v)
	r.siblings[key] = kept
}

// MergeWrite validates a client merge write against the complete-context rule
// and, if valid, applies it as a local causal write.
//
// The context is valid when:
//  1. every named context id is resolvable (known id or explicit clock);
//  2. the join of the context clocks causally dominates every current sibling
//     -- i.e. no live concurrent version is missing from the context and no
//     context entry is a stale ancestor that fails to cover the current state.
func (r *Register) MergeWrite(req MergeRequest) (Version, error) {
	sibs := r.siblings[req.Key]
	if len(sibs) == 0 && len(req.Context) == 0 {
		// First write to the key: empty context is fine.
		return r.LocalWrite(req.Key, req.Value), nil
	}

	contextVC := clock.VectorClock{}
	for _, c := range req.Context {
		vc, err := r.resolveContext(req.Key, c)
		if err != nil {
			return Version{}, err
		}
		clock.MergeInto(contextVC, vc)
	}

	var missing []string
	for _, s := range sibs {
		if !clock.Descends(contextVC, s.VC) {
			missing = append(missing, s.ID)
		}
	}
	if missing != nil {
		// Distinguish "client named an old ancestor" from "client simply never
		// saw a sibling": a stale entry is dominated by the missing sibling.
		for _, c := range req.Context {
			vc, err := r.resolveContext(req.Key, c)
			if err != nil {
				return Version{}, err
			}
			for _, m := range missing {
				for _, s := range sibs {
					if s.ID == m && clock.Descends(s.VC, vc) && !clock.Equal(s.VC, vc) {
						return Version{}, &MergeError{Err: ErrStaleContext, EntryID: c.ID}
					}
				}
			}
		}
		return Version{}, &MergeError{Err: ErrConcurrentNotCovered, Missing: missing}
	}

	return r.LocalWrite(req.Key, req.Value), nil
}

// resolveContext finds the clock for a context entry. An explicit clock is
// trusted only if it matches a known sibling id; otherwise the id must be
// present in the current sibling set.
func (r *Register) resolveContext(key string, c ContextEntry) (clock.VectorClock, error) {
	for _, s := range r.siblings[key] {
		if s.ID == c.ID {
			if c.VC != nil && !clock.Equal(c.VC, s.VC) {
				return nil, &MergeError{Err: ErrUnknownContext, EntryID: c.ID}
			}
			return s.VC, nil
		}
	}
	// Unknown id: the simulator may supply the clock from global history.
	if c.VC != nil {
		return clock.Copy(c.VC), nil
	}
	return nil, &MergeError{Err: ErrUnknownContext, EntryID: c.ID}
}
