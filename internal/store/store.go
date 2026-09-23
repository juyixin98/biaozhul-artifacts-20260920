// Package store persists assembled entries to a local append-only
// JSON-lines file. The file is replayed on open so entries survive a
// process restart; the in-memory index is the source of truth for
// queries and the file is a write-through log.
package store

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"sync"

	"logpipe/internal/merger"
)

// FileStore stores entries in <dir>/entries.jsonl.
type FileStore struct {
	mu      sync.RWMutex
	path    string
	f       *os.File
	w       *bufio.Writer
	entries []merger.Entry
	nextID  int64
}

// Open opens (creating if needed) the store directory and replays the
// existing log.
func Open(dir string) (*FileStore, error) {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, fmt.Errorf("store: create dir: %w", err)
	}
	s := &FileStore{
		path:   filepath.Join(dir, "entries.jsonl"),
		nextID: 1,
	}
	if err := s.replay(); err != nil {
		return nil, err
	}
	f, err := os.OpenFile(s.path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return nil, fmt.Errorf("store: open log: %w", err)
	}
	s.f = f
	s.w = bufio.NewWriter(f)
	return s, nil
}

func (s *FileStore) replay() error {
	f, err := os.Open(s.path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil
		}
		return fmt.Errorf("store: replay open: %w", err)
	}
	defer f.Close()

	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 0, 64*1024), 8*1024*1024)
	var bad int
	for sc.Scan() {
		line := sc.Bytes()
		if len(line) == 0 {
			continue
		}
		var e merger.Entry
		if err := json.Unmarshal(line, &e); err != nil {
			// Tolerate a torn trailing write; count and skip it.
			bad++
			continue
		}
		s.entries = append(s.entries, e)
		if e.ID >= s.nextID {
			s.nextID = e.ID + 1
		}
	}
	if err := sc.Err(); err != nil {
		return fmt.Errorf("store: replay scan: %w", err)
	}
	if bad > 0 {
		fmt.Fprintf(os.Stderr, "store: skipped %d unparseable line(s) replaying %s\n", bad, s.path)
	}
	return nil
}

// Append assigns an id, persists and indexes one entry.
func (s *FileStore) Append(e merger.Entry) (merger.Entry, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	e.ID = s.nextID
	s.nextID++
	data, err := json.Marshal(e)
	if err != nil {
		return merger.Entry{}, fmt.Errorf("store: marshal: %w", err)
	}
	if _, err := s.w.Write(data); err != nil {
		return merger.Entry{}, fmt.Errorf("store: write: %w", err)
	}
	if err := s.w.WriteByte('\n'); err != nil {
		return merger.Entry{}, fmt.Errorf("store: write: %w", err)
	}
	if err := s.w.Flush(); err != nil {
		return merger.Entry{}, fmt.Errorf("store: flush: %w", err)
	}
	if err := s.f.Sync(); err != nil {
		return merger.Entry{}, fmt.Errorf("store: sync: %w", err)
	}
	s.entries = append(s.entries, e)
	return e, nil
}

// Query filters entries by exact source (empty = all) and limits the
// result. Entries are returned in id (ingestion/assembly) order.
func (s *FileStore) Query(source string, limit int) []merger.Entry {
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
	// Defensive: keep contract even if replay order was odd.
	sort.SliceStable(out, func(i, j int) bool { return out[i].ID < out[j].ID })
	return out
}

// Count returns the number of stored entries.
func (s *FileStore) Count() int {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return len(s.entries)
}

// Close flushes and closes the underlying file.
func (s *FileStore) Close() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.f == nil {
		return nil
	}
	if err := s.w.Flush(); err != nil {
		s.f.Close()
		return err
	}
	return s.f.Close()
}
