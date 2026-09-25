// Package store owns all on-disk state of the service.
//
// Layout (all under the configured root, which never contains project
// sources):
//
//	projects/<name>/...            user-supplied project trees (build.json,
//	                               tools, sources) — copied in by the client
//	cache/blobs/<sha256-hex>       content-addressed, write-once artifacts
//	cache/fingerprints/<fp-hex>    one line: record id of a cached execution
//	work/<run>-<action>/           disposable, per-action working directories
//	state/records/<id>.json        signed provenance records
//	state/projects/<name>.json     per-project index (action -> record)
//	state/hmac.key                 HMAC-SHA256 key for record signatures
//
// The cache and work trees are deliberately separate siblings: the cache is
// immutable and shared across runs; work dirs are ephemeral and deleted after
// each action.
package store

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"bis/internal/digest"
	"bis/internal/provenance"
	"bis/internal/safeio"
)

// Store is the on-disk service state.
type Store struct {
	root string

	ProjectsDir string
	CacheDir    string
	BlobsDir    string
	FPDir       string
	WorkDir     string
	StateDir    string
	RecordsDir  string
	IndexDir    string

	key []byte
}

// Index maps a project's actions to the provenance record of their most
// recent execution, and records each action's output digests.
type Index struct {
	Project string                 `json:"project"`
	Records map[string]RecordEntry `json:"records"` // action id -> entry
}

// RecordEntry pins one action execution.
type RecordEntry struct {
	RecordID digest.Digest            `json:"record_id"`
	Outputs  map[string]digest.Digest `json:"outputs"` // output path -> digest
}

// Open (or initializes) a store rooted at root.
func Open(root string) (*Store, error) {
	s := &Store{root: root}
	s.ProjectsDir = filepath.Join(root, "projects")
	s.CacheDir = filepath.Join(root, "cache")
	s.BlobsDir = filepath.Join(s.CacheDir, "blobs")
	s.FPDir = filepath.Join(s.CacheDir, "fingerprints")
	s.WorkDir = filepath.Join(root, "work")
	s.StateDir = filepath.Join(root, "state")
	s.RecordsDir = filepath.Join(s.StateDir, "records")
	s.IndexDir = filepath.Join(s.StateDir, "projects")
	for _, d := range []string{s.ProjectsDir, s.BlobsDir, s.FPDir, s.WorkDir, s.RecordsDir, s.IndexDir} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			return nil, err
		}
	}
	if err := s.loadOrCreateKey(); err != nil {
		return nil, err
	}
	return s, nil
}

// Key returns the HMAC signing key.
func (s *Store) Key() []byte { return s.key }

func (s *Store) loadOrCreateKey() error {
	p := filepath.Join(s.StateDir, "hmac.key")
	if b, err := os.ReadFile(p); err == nil {
		if len(b) != 32 {
			return fmt.Errorf("hmac.key has invalid length %d", len(b))
		}
		s.key = b
		return nil
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	key := make([]byte, 32)
	if _, err := rand.Read(key); err != nil {
		return err
	}
	if err := os.WriteFile(p, key, 0o600); err != nil {
		return err
	}
	s.key = key
	return nil
}

// ProjectRoot returns the directory of a project.
func (s *Store) ProjectRoot(name string) string {
	return filepath.Join(s.ProjectsDir, name)
}

// ResolveProjectPath resolves rel inside a project directory with full
// symlink-containment checks.
func (s *Store) ResolveProjectPath(name, rel string) (string, error) {
	return safeio.ResolveWithin(s.ProjectRoot(name), rel)
}

// PutBlob copies the file at src into the content-addressed cache. It is
// idempotent: if the blob already exists, the existing entry is reused.
// Returns the digest.
func (s *Store) PutBlob(src string) (digest.Digest, error) {
	d, err := digest.OfFile(src)
	if err != nil {
		return digest.Digest{}, err
	}
	dst := filepath.Join(s.BlobsDir, d.Hex)
	if _, err := os.Stat(dst); err == nil {
		return d, nil // already cached; the cache is immutable
	} else if !errors.Is(err, os.ErrNotExist) {
		return digest.Digest{}, err
	}
	f, err := os.Open(src)
	if err != nil {
		return digest.Digest{}, err
	}
	defer f.Close()
	tmp := dst + ".tmp"
	out, err := os.OpenFile(tmp, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0o444)
	if err != nil {
		return digest.Digest{}, err
	}
	h := sha256.New()
	if _, err := io.Copy(io.MultiWriter(out, h), f); err != nil {
		out.Close()
		os.Remove(tmp)
		return digest.Digest{}, err
	}
	if err := out.Close(); err != nil {
		os.Remove(tmp)
		return digest.Digest{}, err
	}
	got := hex.EncodeToString(h.Sum(nil))
	if got != d.Hex {
		os.Remove(tmp)
		return digest.Digest{}, fmt.Errorf("blob digest changed during copy")
	}
	if err := os.Link(tmp, dst); err != nil {
		// Fall back to rename (e.g. cross-device edge cases).
		if rerr := os.Rename(tmp, dst); rerr != nil {
			return digest.Digest{}, rerr
		}
	} else {
		os.Remove(tmp)
	}
	return d, nil
}

// BlobPath returns the cache path of a digest.
func (s *Store) BlobPath(d digest.Digest) string {
	return filepath.Join(s.BlobsDir, d.Hex)
}

// HasBlob reports whether an artifact is present in the cache.
func (s *Store) HasBlob(d digest.Digest) bool {
	_, err := os.Stat(s.BlobPath(d))
	return err == nil
}

// PutRecord validates, signs and stores a provenance record, returning its
// content id. The caller must not have set Sig; it is computed here.
func (s *Store) PutRecord(r *provenance.Record) (digest.Digest, error) {
	r.Schema = provenance.CanonicalVersion
	r.Sig = ""
	if err := r.Sign(s.key); err != nil {
		return digest.Digest{}, err
	}
	id, err := provenance.ContentID(r)
	if err != nil {
		return digest.Digest{}, err
	}
	b, err := json.MarshalIndent(r, "", "  ")
	if err != nil {
		return digest.Digest{}, err
	}
	p := filepath.Join(s.RecordsDir, id.Hex+".json")
	if _, err := os.Stat(p); errors.Is(err, os.ErrNotExist) {
		if err := os.WriteFile(p, b, 0o444); err != nil {
			return digest.Digest{}, err
		}
	} else if err != nil {
		return digest.Digest{}, err
	}
	return id, nil
}

// GetRecord loads a record by id.
func (s *Store) GetRecord(id digest.Digest) (*provenance.Record, error) {
	p := filepath.Join(s.RecordsDir, id.Hex+".json")
	b, err := os.ReadFile(p)
	if err != nil {
		return nil, err
	}
	var r provenance.Record
	if err := json.Unmarshal(b, &r); err != nil {
		return nil, fmt.Errorf("record %s: %w", id.Hex[:12], err)
	}
	return &r, nil
}

// FingerprintLookup returns the record id previously stored for a fingerprint.
func (s *Store) FingerprintLookup(fp digest.Digest) (digest.Digest, bool) {
	b, err := os.ReadFile(filepath.Join(s.FPDir, fp.Hex))
	if err != nil {
		return digest.Digest{}, false
	}
	id, err := digest.Parse(strings.TrimSpace(string(b)))
	if err != nil {
		return digest.Digest{}, false
	}
	return id, true
}

// FingerprintStore records that fingerprint fp was executed as record id.
func (s *Store) FingerprintStore(fp, id digest.Digest) error {
	return os.WriteFile(filepath.Join(s.FPDir, fp.Hex), []byte(id.String()+"\n"), 0o444)
}

// LoadIndex returns the (possibly empty) index of a project.
func (s *Store) LoadIndex(project string) *Index {
	idx := &Index{Project: project, Records: map[string]RecordEntry{}}
	b, err := os.ReadFile(s.indexPath(project))
	if err != nil {
		return idx
	}
	var stored Index
	if err := json.Unmarshal(b, &stored); err == nil && stored.Records != nil {
		return &stored
	}
	return idx
}

// SaveIndex atomically persists the project index.
func (s *Store) SaveIndex(idx *Index) error {
	b, err := json.MarshalIndent(idx, "", "  ")
	if err != nil {
		return err
	}
	tmp := s.indexPath(idx.Project) + ".tmp"
	if err := os.WriteFile(tmp, b, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, s.indexPath(idx.Project))
}

func (s *Store) indexPath(project string) string {
	return filepath.Join(s.IndexDir, project+".json")
}

// NewWorkDir creates a fresh disposable working directory for one action
// execution, under the work tree (never the cache).
func (s *Store) NewWorkDir(runTag, actionID string) (string, error) {
	name := fmt.Sprintf("%s-%s", runTag, strings.ReplaceAll(actionID, "/", "_"))
	d := filepath.Join(s.WorkDir, name)
	if err := os.MkdirAll(d, 0o755); err != nil {
		return "", err
	}
	return d, nil
}

// RemoveWorkDir deletes a disposable working directory.
func (s *Store) RemoveWorkDir(d string) error {
	// Only allow deleting things directly inside the work tree.
	rel, err := filepath.Rel(s.WorkDir, d)
	if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(os.PathSeparator)) {
		return fmt.Errorf("refusing to remove work dir outside work tree: %s", d)
	}
	return os.RemoveAll(d)
}
