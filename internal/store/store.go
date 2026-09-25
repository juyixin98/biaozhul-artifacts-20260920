// Package store provides a tiny JSON-file-backed persistence layer for
// named histograms. It is a local sample, not a production database.
package store

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"sync"

	"histmerge/internal/histogram"
)

var ErrNotFound = errors.New("histogram not found")

// Store keeps histograms in memory and mirrors them to one JSON file.
type Store struct {
	mu   sync.RWMutex
	path string
	hs   map[string]*histogram.Histogram
}

// Open loads the store from path (missing file = empty store).
func Open(path string) (*Store, error) {
	s := &Store{path: path, hs: make(map[string]*histogram.Histogram)}
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return s, nil
	}
	if err != nil {
		return nil, err
	}
	if len(data) == 0 {
		return s, nil
	}
	if err := json.Unmarshal(data, &s.hs); err != nil {
		return nil, fmt.Errorf("load store %s: %w", path, err)
	}
	for name, h := range s.hs {
		if err := h.Validate(); err != nil {
			return nil, fmt.Errorf("stored histogram %q invalid: %w", name, err)
		}
	}
	return s, nil
}

// Put validates and stores a histogram under its name, then persists.
func (s *Store) Put(h *histogram.Histogram) error {
	if h.Name == "" {
		return errors.New("histogram name must not be empty")
	}
	if err := h.Validate(); err != nil {
		return err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.hs[h.Name] = h.Clone()
	return s.saveLocked()
}

// Get returns a copy of the named histogram.
func (s *Store) Get(name string) (*histogram.Histogram, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	h, ok := s.hs[name]
	if !ok {
		return nil, fmt.Errorf("%w: %q", ErrNotFound, name)
	}
	return h.Clone(), nil
}

// List returns the sorted names of all stored histograms.
func (s *Store) List() []string {
	s.mu.RLock()
	defer s.mu.RUnlock()
	names := make([]string, 0, len(s.hs))
	for n := range s.hs {
		names = append(names, n)
	}
	sort.Strings(names)
	return names
}

// saveLocked writes the whole store atomically (tmp file + rename).
func (s *Store) saveLocked() error {
	if s.path == "" {
		return nil
	}
	data, err := json.MarshalIndent(s.hs, "", "  ")
	if err != nil {
		return err
	}
	tmp := s.path + ".tmp"
	if err := os.MkdirAll(filepath.Dir(s.path), 0o755); err != nil {
		return err
	}
	if err := os.WriteFile(tmp, data, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, s.path)
}
