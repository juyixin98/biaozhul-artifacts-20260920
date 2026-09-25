package provenance

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"sync"
)

// ErrNotFound is returned by stores and registries for unknown keys.
var ErrNotFound = errors.New("not found")

// CAS is a content-addressed blob store. Blobs are stored at
// <root>/ab/cdef... (split by the first two hex chars). It is safe to share
// across processes only when used from a single process (the service holds a
// coordinating mutex via the registries).
type CAS struct {
	root string
	mu   sync.RWMutex
}

// NewCAS opens (creating if needed) a CAS rooted at root.
func NewCAS(root string) (*CAS, error) {
	if err := os.MkdirAll(root, 0o755); err != nil {
		return nil, fmt.Errorf("cas: create root %s: %w", root, err)
	}
	return &CAS{root: root}, nil
}

func (c *CAS) path(d Digest) string {
	h := d.Hex()
	return filepath.Join(c.root, h[:2], h[2:])
}

// Put stores b and returns its digest. Temp-file + rename makes a concurrent
// write of identical content harmless.
func (c *CAS) Put(b []byte) (Digest, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	d := DigestBytes(b)
	p := c.path(d)
	if _, err := os.Stat(p); err == nil {
		return d, nil
	}
	if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
		return "", err
	}
	tmp, err := os.CreateTemp(filepath.Dir(p), ".tmp-*")
	if err != nil {
		return "", err
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName)
	if _, err := tmp.Write(b); err != nil {
		tmp.Close()
		return "", err
	}
	if err := tmp.Close(); err != nil {
		return "", err
	}
	if err := os.Chmod(tmpName, 0o644); err != nil {
		return "", err
	}
	if err := os.Rename(tmpName, p); err != nil {
		return "", err
	}
	return d, nil
}

// Has reports whether a blob with digest d exists.
func (c *CAS) Has(d Digest) bool {
	c.mu.RLock()
	defer c.mu.RUnlock()
	_, err := os.Stat(c.path(d))
	return err == nil
}

// Get returns the blob bytes.
func (c *CAS) Get(d Digest) ([]byte, error) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	b, err := os.ReadFile(c.path(d))
	if errors.Is(err, os.ErrNotExist) {
		return nil, fmt.Errorf("%w: blob %s", ErrNotFound, d)
	}
	return b, err
}

// Stat returns file info for a stored blob.
func (c *CAS) Stat(d Digest) (os.FileInfo, error) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	fi, err := os.Stat(c.path(d))
	if errors.Is(err, os.ErrNotExist) {
		return nil, fmt.Errorf("%w: blob %s", ErrNotFound, d)
	}
	return fi, err
}

// SourceRegistry is an in-memory index of logical source paths to metadata;
// the bytes live in a CAS.
type SourceRegistry struct {
	cas *CAS
	mu  sync.RWMutex
	by  map[string]Source
}

func NewSourceRegistry(cas *CAS) *SourceRegistry {
	return &SourceRegistry{cas: cas, by: map[string]Source{}}
}

// CAS returns the backing blob store.
func (r *SourceRegistry) CAS() *CAS { return r.cas }

// Register stores content and indexes it under path. Re-registering an
// existing path replaces the pointer (old blobs are retained).
func (r *SourceRegistry) Register(path string, content []byte, now string) (Source, error) {
	if path == "" || filepath.IsAbs(path) {
		return Source{}, fmt.Errorf("source path must be a non-empty relative path")
	}
	clean := filepath.ToSlash(filepath.Clean("/" + path))[1:]
	if clean == "." || clean == "" {
		return Source{}, fmt.Errorf("invalid source path %q", path)
	}
	d, err := r.cas.Put(content)
	if err != nil {
		return Source{}, err
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	s := Source{Path: clean, Digest: d, Size: int64(len(content)), CreatedAt: now}
	r.by[clean] = s
	return s, nil
}

func (r *SourceRegistry) Get(path string) (Source, error) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	s, ok := r.by[path]
	if !ok {
		return Source{}, fmt.Errorf("%w: source %q", ErrNotFound, path)
	}
	return s, nil
}

// List returns all sources ordered by path.
func (r *SourceRegistry) List() []Source {
	r.mu.RLock()
	defer r.mu.RUnlock()
	out := make([]Source, 0, len(r.by))
	for _, s := range r.by {
		out = append(out, s)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Path < out[j].Path })
	return out
}

// ArtifactRegistry indexes artifact IDs. Bytes live in the artifact CAS,
// which is deliberately a separate directory from source storage.
type ArtifactRegistry struct {
	cas *CAS
	mu  sync.RWMutex
	by  map[string]Artifact
}

func NewArtifactRegistry(cas *CAS) *ArtifactRegistry {
	return &ArtifactRegistry{cas: cas, by: map[string]Artifact{}}
}

// Put stores the blob, then indexes the artifact metadata.
func (r *ArtifactRegistry) Put(id string, content []byte, meta func(d Digest, size int64) Artifact) (Artifact, error) {
	d, err := r.cas.Put(content)
	if err != nil {
		return Artifact{}, err
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if existing, ok := r.by[id]; ok {
		return existing, nil
	}
	a := meta(d, int64(len(content)))
	if a.Digest == "" {
		a.Digest = d
	}
	r.by[id] = a
	return a, nil
}

func (r *ArtifactRegistry) Get(id string) (Artifact, error) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	a, ok := r.by[id]
	if !ok {
		return Artifact{}, fmt.Errorf("%w: artifact %s", ErrNotFound, id)
	}
	return a, nil
}

// List returns all artifacts ordered by ID.
func (r *ArtifactRegistry) List() []Artifact {
	r.mu.RLock()
	defer r.mu.RUnlock()
	out := make([]Artifact, 0, len(r.by))
	for _, a := range r.by {
		out = append(out, a)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ID < out[j].ID })
	return out
}

// CAS returns the backing store (used by verify/reproduce to read bytes).
func (r *ArtifactRegistry) CAS() *CAS { return r.cas }

// Update replaces metadata for an existing artifact (e.g. after its
// attestation hash is appended).
func (r *ArtifactRegistry) Update(a Artifact) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	if _, ok := r.by[a.ID]; !ok {
		return fmt.Errorf("%w: artifact %s", ErrNotFound, a.ID)
	}
	r.by[a.ID] = a
	return nil
}

// Persist / load the source and artifact indexes to simple JSON sidecar files
// so that restarting the service keeps the registry consistent with existing
// CAS directories. The attestation log is the authoritative history; these
// are only lookup caches.

func (r *SourceRegistry) Save(path string) error {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return writeJSON(path, r.by)
}

func (r *SourceRegistry) Load(path string) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	by := map[string]Source{}
	if err := readJSON(path, &by); err != nil {
		return err
	}
	r.by = by
	return nil
}

func (r *ArtifactRegistry) Save(path string) error {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return writeJSON(path, r.by)
}

func (r *ArtifactRegistry) Load(path string) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	by := map[string]Artifact{}
	if err := readJSON(path, &by); err != nil {
		return err
	}
	r.by = by
	return nil
}

func writeJSON(path string, v any) error {
	b, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, b, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

func readJSON(path string, v any) error {
	b, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	if len(b) == 0 {
		return nil
	}
	return json.Unmarshal(b, v)
}
