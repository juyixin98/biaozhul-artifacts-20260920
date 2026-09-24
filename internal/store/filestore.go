// Package store persists DAG snapshots as JSON files, one per DAG.
// Writes are atomic (temp file + rename) and serialized per file, so a crash
// can never leave a torn snapshot: at worst the previous complete snapshot is
// read back.
package store

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"sync"
)

var idRe = regexp.MustCompile(`^[A-Za-z0-9_-]{1,64}$`)

// FileStore keeps snapshots in a single directory.
type FileStore struct {
	dir string
	mu  sync.Mutex // serializes writers; reads are lock-free
}

// NewFileStore creates dir (with parents) if needed.
func NewFileStore(dir string) (*FileStore, error) {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, fmt.Errorf("create data dir %q: %w", dir, err)
	}
	return &FileStore{dir: dir}, nil
}

func (s *FileStore) path(id string) string {
	return filepath.Join(s.dir, id+".json")
}

// Save atomically writes a snapshot.
func (s *FileStore) Save(id string, snap any) error {
	if !idRe.MatchString(id) {
		return fmt.Errorf("invalid snapshot id %q", id)
	}
	data, err := json.MarshalIndent(snap, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal snapshot %s: %w", id, err)
	}
	s.mu.Lock()
	defer s.mu.Unlock()

	final := s.path(id)
	tmp := final + ".tmp"
	if err := os.WriteFile(tmp, data, 0o644); err != nil {
		return fmt.Errorf("write snapshot %s: %w", id, err)
	}
	if err := os.Rename(tmp, final); err != nil {
		return fmt.Errorf("commit snapshot %s: %w", id, err)
	}
	return nil
}

// Load reads a snapshot into out (a *Snapshot). ErrNotFound wraps os.ErrNotExist.
func (s *FileStore) Load(id string, out any) error {
	data, err := os.ReadFile(s.path(id))
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return ErrNotFound
		}
		return err
	}
	if err := json.Unmarshal(data, out); err != nil {
		return fmt.Errorf("corrupt snapshot %s: %w", id, err)
	}
	return nil
}

// ErrNotFound is returned when no snapshot exists for an id.
var ErrNotFound = errors.New("snapshot not found")

// List returns all snapshot ids in lexicographic order.
func (s *FileStore) List() ([]string, error) {
	entries, err := os.ReadDir(s.dir)
	if err != nil {
		return nil, err
	}
	var ids []string
	for _, e := range entries {
		name := e.Name()
		if e.IsDir() || filepath.Ext(name) != ".json" {
			continue
		}
		ids = append(ids, name[:len(name)-len(".json")])
	}
	sort.Strings(ids)
	return ids, nil
}
