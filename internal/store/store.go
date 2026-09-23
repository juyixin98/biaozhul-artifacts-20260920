// Package store holds immutable admission reports. The JSONL implementation
// is append-only by construction: writes use O_APPEND and there is no update
// or delete API. Re-evaluating the same image always appends a NEW report
// with a NEW id; an old report is never overwritten.
package store

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"sync"

	"mirror-admission/internal/model"
)

// ErrNotFound is returned for unknown report ids.
var ErrNotFound = errors.New("report not found")

// Memory is an in-memory Store for tests.
type Memory struct {
	mu      sync.Mutex
	reports []*model.Report
}

// NewMemory returns an empty memory store.
func NewMemory() *Memory { return &Memory{} }

func (m *Memory) Save(_ context.Context, r *model.Report) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	for _, existing := range m.reports {
		if existing.ID == r.ID {
			// Defensive: random 128-bit ids should not collide, but a
			// collision must overwrite nothing.
			return fmt.Errorf("report id %s already exists; refusing to overwrite", r.ID)
		}
	}
	cp := *r
	m.reports = append(m.reports, &cp)
	return nil
}

func (m *Memory) Get(_ context.Context, id string) (*model.Report, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	for _, r := range m.reports {
		if r.ID == id {
			cp := *r
			return &cp, nil
		}
	}
	return nil, ErrNotFound
}

func (m *Memory) List(_ context.Context) ([]model.Report, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := make([]model.Report, 0, len(m.reports))
	for _, r := range m.reports {
		out = append(out, *r)
	}
	return out, nil
}

// JSONL is the on-disk append-only store.
type JSONL struct {
	mu   sync.Mutex
	path string
}

// NewJSONL opens (creating if needed) the reports file.
func NewJSONL(path string) (*JSONL, error) {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return nil, err
	}
	return &JSONL{path: path}, f.Close()
}

// Save appends one JSON line. O_APPEND makes the write atomic-ish per write
// call; the mutex serialises within this process.
func (s *JSONL) Save(_ context.Context, r *model.Report) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	// Never overwrite: if the id is already present, refuse.
	if _, err := s.getLocked(r.ID); err == nil {
		return fmt.Errorf("report id %s already exists on disk; refusing overwrite", r.ID)
	} else if !errors.Is(err, ErrNotFound) {
		return err
	}

	line, err := json.Marshal(r)
	if err != nil {
		return err
	}
	line = append(line, '\n')
	f, err := os.OpenFile(s.path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		return err
	}
	defer f.Close()
	if _, err := f.Write(line); err != nil {
		return err
	}
	return f.Sync()
}

func (s *JSONL) Get(ctx context.Context, id string) (*model.Report, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.getLocked(id)
}

func (s *JSONL) getLocked(id string) (*model.Report, error) {
	all, err := s.listLocked()
	if err != nil {
		return nil, err
	}
	for i := range all {
		if all[i].ID == id {
			return &all[i], nil
		}
	}
	return nil, ErrNotFound
}

func (s *JSONL) List(_ context.Context) ([]model.Report, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.listLocked()
}

func (s *JSONL) listLocked() ([]model.Report, error) {
	f, err := os.Open(s.path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	var out []model.Report
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 1024*1024), 16*1024*1024)
	for sc.Scan() {
		line := sc.Bytes()
		if len(line) == 0 {
			continue
		}
		var r model.Report
		if err := json.Unmarshal(line, &r); err != nil {
			return nil, fmt.Errorf("corrupt report line in %s: %w", s.path, err)
		}
		out = append(out, r)
	}
	return out, sc.Err()
}
