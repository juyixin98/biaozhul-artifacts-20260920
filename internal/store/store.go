// Package store persists assembled log entries as JSON Lines on disk
// and serves queries from an in-memory index rebuilt on startup.
package store

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sync"

	"logmerge/internal/merger"
)

// Store appends entries to a JSONL file and answers queries.
type Store struct {
	mu      sync.RWMutex
	path    string
	entries []merger.Entry
}

// Open creates or loads the store at path. Existing entries are loaded
// into memory so a process restart loses nothing already flushed.
func Open(path string) (*Store, error) {
	if dir := filepath.Dir(path); dir != "" {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return nil, fmt.Errorf("create store dir: %w", err)
		}
	}
	s := &Store{path: path}
	f, err := os.Open(path)
	if err != nil {
		if os.IsNotExist(err) {
			return s, nil
		}
		return nil, fmt.Errorf("open store: %w", err)
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 0, 1<<20), 16<<20)
	for sc.Scan() {
		line := sc.Bytes()
		if len(line) == 0 {
			continue
		}
		var e merger.Entry
		if err := json.Unmarshal(line, &e); err != nil {
			return nil, fmt.Errorf("corrupt store line: %w", err)
		}
		s.entries = append(s.entries, e)
	}
	return s, sc.Err()
}

// Append persists an entry (fsync'd) and indexes it.
func (s *Store) Append(e merger.Entry) error {
	b, err := json.Marshal(e)
	if err != nil {
		return err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	f, err := os.OpenFile(s.path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return err
	}
	if _, err := f.Write(append(b, '\n')); err != nil {
		f.Close()
		return err
	}
	if err := f.Sync(); err != nil {
		f.Close()
		return err
	}
	if err := f.Close(); err != nil {
		return err
	}
	s.entries = append(s.entries, e)
	return nil
}

// List returns stored entries, newest last. If source is non-empty only
// that source's entries are returned. limit <= 0 means no limit.
func (s *Store) List(source string, limit int) []merger.Entry {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make([]merger.Entry, 0, len(s.entries))
	for _, e := range s.entries {
		if source != "" && e.Source != source {
			continue
		}
		out = append(out, e)
	}
	if limit > 0 && len(out) > limit {
		out = out[len(out)-limit:]
	}
	return out
}
