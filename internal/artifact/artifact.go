// Package artifact models immutable binary artifacts and their selected
// representations.
//
// An artifact has one canonical byte sequence (the "selected representation"
// that Range units are measured against). The server offers two
// representations: identity (raw bytes) and gzip. Content-Length and range
// offsets always refer to the representation actually selected by content
// negotiation, never to the underlying resource data.
package artifact

import (
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"sync"
	"time"
)

// Representation identifiers used in Accept-Encoding / Content-Encoding.
const (
	RepIdentity = "identity"
	RepGzip     = "gzip"
)

var (
	// ErrUnknownArtifact means the id is not registered.
	ErrUnknownArtifact = errors.New("artifact: unknown artifact")
	// ErrUnknownEncoding means the representation is not offered.
	ErrUnknownEncoding = errors.New("artifact: unknown representation")
)

// Artifact is an immutable binary artifact.
type Artifact struct {
	id           string
	raw          []byte // canonical bytes; never mutated after construction
	etag         string // strong validator for the identity representation
	gzipED       []byte // lazily computed gzip representation
	gzipEtag     string // strong validator for the gzip representation
	gzipMu       sync.Once
	lastModified time.Time
}

// New constructs an artifact from raw bytes. The provided slice is copied so
// later caller mutation cannot change the artifact. The ETag is derived from
// the content, making equal content share a validator (strong, opaque).
// Last-Modified defaults to construction time; use NewWithMetadata to pin a
// deterministic value in tests.
func New(id string, raw []byte) *Artifact {
	return NewWithMetadata(id, raw, time.Now().UTC())
}

// NewWithMetadata is New with an explicit last-modified timestamp.
func NewWithMetadata(id string, raw []byte, lastModified time.Time) *Artifact {
	cp := append([]byte(nil), raw...)
	sum := sha256.Sum256(cp)
	return &Artifact{
		id:           id,
		raw:          cp,
		etag:         `"` + hex.EncodeToString(sum[:16]) + `"`,
		lastModified: lastModified.UTC().Truncate(time.Second),
	}
}

// ID returns the artifact identifier.
func (a *Artifact) ID() string { return a.id }

// ETag returns the strong entity tag for the identity representation,
// including the surrounding double quotes.
func (a *Artifact) ETag() string { return a.etag }

// ETagFor returns the strong validator for the selected representation.
// Identity and gzip are different selected representations with different
// byte boundaries, so they carry distinct strong validators.
func (a *Artifact) ETagFor(encoding string) (string, error) {
	if encoding == RepIdentity {
		return a.etag, nil
	}
	if encoding != RepGzip {
		return "", fmt.Errorf("%w: %q", ErrUnknownEncoding, encoding)
	}
	a.GzipBytes() // force lazy population
	return a.gzipEtag, nil
}

// LastModified returns the representation's Last-Modified timestamp.
func (a *Artifact) LastModified() time.Time { return a.lastModified }

// ContentHash returns the hex SHA-256 of the canonical bytes; the client
// uses it to verify reassembled downloads.
func (a *Artifact) ContentHash() string {
	sum := sha256.Sum256(a.raw)
	return hex.EncodeToString(sum[:])
}

// GzipBytes returns the gzip encoding of the canonical bytes.
func (a *Artifact) GzipBytes() []byte {
	a.gzipMu.Do(func() {
		var buf bytes.Buffer
		w := gzip.NewWriter(&buf)
		// bytes.Buffer.Write never errors; gzip.Close flushes the trailer.
		_, _ = w.Write(a.raw)
		_ = w.Close()
		a.gzipED = buf.Bytes()
		sum := sha256.Sum256(a.gzipED)
		a.gzipEtag = `"` + hex.EncodeToString(sum[:16]) + `"`
	})
	return append([]byte(nil), a.gzipED...)
}

// Representation returns the selected representation's bytes, copying them
// so callers cannot mutate artifact state.
func (a *Artifact) Representation(encoding string) ([]byte, error) {
	switch encoding {
	case RepIdentity:
		return append([]byte(nil), a.raw...), nil
	case RepGzip:
		return a.GzipBytes(), nil
	default:
		return nil, fmt.Errorf("%w: %q", ErrUnknownEncoding, encoding)
	}
}

// Metadata describes a selectable representation.
type Metadata struct {
	Encoding   string // identity or gzip
	Length     int64
	ETag       string
	MediaType  string
	GzipVaries bool // true when Vary: Accept-Encoding is appropriate
}

// Describe returns metadata for a representation.
func (a *Artifact) Describe(encoding string) (Metadata, error) {
	body, err := a.Representation(encoding)
	if err != nil {
		return Metadata{}, err
	}
	etag, err := a.ETagFor(encoding)
	if err != nil {
		return Metadata{}, err
	}
	return Metadata{
		Encoding:   encoding,
		Length:     int64(len(body)),
		ETag:       etag,
		MediaType:  "application/octet-stream",
		GzipVaries: encoding == RepGzip,
	}, nil
}

// Registry holds loaded artifacts.
type Registry struct {
	mu        sync.RWMutex
	artifacts map[string]*Artifact
}

// NewRegistry returns an empty registry.
func NewRegistry() *Registry {
	return &Registry{artifacts: make(map[string]*Artifact)}
}

// Put registers an artifact.
func (r *Registry) Put(a *Artifact) {
	r.mu.Lock()
	r.artifacts[a.id] = a
	r.mu.Unlock()
}

// Get fetches an artifact by id.
func (r *Registry) Get(_ context.Context, id string) (*Artifact, error) {
	r.mu.RLock()
	a, ok := r.artifacts[id]
	r.mu.RUnlock()
	if !ok {
		return nil, fmt.Errorf("%w: %s", ErrUnknownArtifact, id)
	}
	return a, nil
}

// IDs returns registered artifact ids, unsorted.
func (r *Registry) IDs() []string {
	r.mu.RLock()
	ids := make([]string, 0, len(r.artifacts))
	for id := range r.artifacts {
		ids = append(ids, id)
	}
	r.mu.RUnlock()
	return ids
}

// Gunzip is a helper for tests and clients that need to decode a gzip
// representation back to canonical bytes.
func Gunzip(body []byte) ([]byte, error) {
	zr, err := gzip.NewReader(bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	defer zr.Close()
	return io.ReadAll(zr)
}
