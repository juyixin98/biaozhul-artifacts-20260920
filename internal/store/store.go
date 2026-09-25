// Package store persists traces to the local filesystem as one JSON
// file per trace. Writes are atomic (temp file + rename) and guarded by
// a mutex so the sample backend is safe for concurrent requests.
package store

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"

	"cpathtrace/internal/model"
)

// ErrNotFound is returned when a trace id does not exist.
var ErrNotFound = errors.New("trace not found")

// FileStore is a directory-backed trace store.
type FileStore struct {
	dir string
	mu  sync.Mutex
}

// NewFileStore creates dir (if needed) and returns a store rooted there.
func NewFileStore(dir string) (*FileStore, error) {
	if dir == "" {
		return nil, errors.New("store directory must not be empty")
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, fmt.Errorf("create store dir: %w", err)
	}
	return &FileStore{dir: dir}, nil
}

func (s *FileStore) path(traceID string) (string, error) {
	if traceID == "" || traceID != filepath.Base(traceID) || strings.ContainsAny(traceID, `/\`) {
		return "", fmt.Errorf("invalid trace id %q", traceID)
	}
	return filepath.Join(s.dir, traceID+".json"), nil
}

// Save writes one trace. Creating and overwriting are both allowed.
func (s *FileStore) Save(t model.Trace) error {
	p, err := s.path(t.TraceID)
	if err != nil {
		return err
	}
	s.mu.Lock()
	defer s.mu.Unlock()

	data, err := json.MarshalIndent(t, "", "  ")
	if err != nil {
		return err
	}
	tmp, err := os.CreateTemp(s.dir, ".tmp-"+t.TraceID+"-*")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName)
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(tmpName, p)
}

// Get loads one trace.
func (s *FileStore) Get(traceID string) (model.Trace, error) {
	p, err := s.path(traceID)
	if err != nil {
		return model.Trace{}, err
	}
	s.mu.Lock()
	defer s.mu.Unlock()

	data, err := os.ReadFile(p)
	if errors.Is(err, os.ErrNotExist) {
		return model.Trace{}, ErrNotFound
	}
	if err != nil {
		return model.Trace{}, err
	}
	var t model.Trace
	if err := json.Unmarshal(data, &t); err != nil {
		return model.Trace{}, err
	}
	return t, nil
}

// Delete removes one trace. Returns ErrNotFound if absent.
func (s *FileStore) Delete(traceID string) error {
	p, err := s.path(traceID)
	if err != nil {
		return err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := os.Remove(p); errors.Is(err, os.ErrNotExist) {
		return ErrNotFound
	} else if err != nil {
		return err
	}
	return nil
}

// List returns the trace ids currently stored, sorted.
func (s *FileStore) List() ([]string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	entries, err := os.ReadDir(s.dir)
	if err != nil {
		return nil, err
	}
	var ids []string
	for _, e := range entries {
		if e.IsDir() {
			continue
		}
		name := e.Name()
		if strings.HasSuffix(name, ".json") {
			ids = append(ids, strings.TrimSuffix(name, ".json"))
		}
	}
	sort.Strings(ids)
	return ids, nil
}
