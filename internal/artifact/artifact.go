// Package artifact models immutable binary representations served over HTTP.
//
// An Artifact is a selected representation (RFC 9110 §3.2): once created its
// bytes, validator (ETag) and metadata never change. The store hands out
// copies of the byte slice so callers cannot mutate the underlying array.
package artifact

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"sort"
	"sync"
	"time"
)

// Artifact is an immutable binary resource representation.
type Artifact struct {
	id           string
	data         []byte
	contentType  string
	etag         string
	lastModified time.Time
}

// Validator returns a strong ETag (quoted) derived from the SHA-256 of data.
// Derived representations use the same rule, guaranteeing that distinct
// selected representations carry distinct validators.
func Validator(data []byte) string {
	sum := sha256.Sum256(data)
	return `"` + hex.EncodeToString(sum[:16]) + `"`
}

// New builds an artifact, deriving a strong ETag from a SHA-256 of the
// content. lastModified is truncated to whole seconds because HTTP-date has
// no sub-second precision.
func New(id string, data []byte, contentType string, lastModified time.Time) *Artifact {
	etag := Validator(data)
	cp := append([]byte(nil), data...)
	return &Artifact{
		id:           id,
		data:         cp,
		contentType:  contentType,
		etag:         etag,
		lastModified: lastModified.UTC().Truncate(time.Second),
	}
}

// ID returns the store key.
func (a *Artifact) ID() string { return a.id }

// Len returns the representation length in bytes.
func (a *Artifact) Len() int64 { return int64(len(a.data)) }

// ContentType returns the representation media type.
func (a *Artifact) ContentType() string { return a.contentType }

// ETag returns the strong validator, including double quotes.
func (a *Artifact) ETag() string { return a.etag }

// LastModified returns the HTTP-date instant.
func (a *Artifact) LastModified() time.Time { return a.lastModified }

// Bytes returns a defensive copy of the full representation.
func (a *Artifact) Bytes() []byte {
	out := make([]byte, len(a.data))
	copy(out, a.data)
	return out
}

// Slice returns a copy of data[start:end]; end is exclusive. Callers must
// guarantee 0 <= start <= end <= Len.
func (a *Artifact) Slice(start, end int64) []byte {
	if start < 0 || end < start || end > a.Len() {
		panic(fmt.Sprintf("artifact: invalid slice [%d,%d) of %d", start, end, a.Len()))
	}
	out := make([]byte, end-start)
	copy(out, a.data[start:end])
	return out
}

// ErrNotFound indicates an unknown artifact id.
var ErrNotFound = errors.New("artifact not found")

// Store is an in-memory, concurrency-safe registry of immutable artifacts.
type Store struct {
	mu        sync.RWMutex
	artifacts map[string]*Artifact
}

// NewStore creates an empty store.
func NewStore() *Store {
	return &Store{artifacts: make(map[string]*Artifact)}
}

// Put registers a; it returns the store so calls can be chained in setup.
func (s *Store) Put(a *Artifact) *Store {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.artifacts[a.id] = a
	return s
}

// Get returns a read-only artifact handle or ErrNotFound.
func (s *Store) Get(id string) (*Artifact, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	a, ok := s.artifacts[id]
	if !ok {
		return nil, fmt.Errorf("%w: %q", ErrNotFound, id)
	}
	return a, nil
}

// IDs returns sorted artifact ids, mainly for the index handler and tests.
func (s *Store) IDs() []string {
	s.mu.RLock()
	defer s.mu.RUnlock()
	ids := make([]string, 0, len(s.artifacts))
	for id := range s.artifacts {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	return ids
}
