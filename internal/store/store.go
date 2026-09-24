package store

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"sync"
	"time"

	"dagexec/internal/dag"
)

// Current file format version.
const version = 1

// snapshot is the on-disk envelope.
type snapshot struct {
	Version int                      `json:"version"`
	SavedAt time.Time                `json:"saved_at"`
	DAGs    map[string]*dag.DAGState `json:"dags"`
}

// FileStore keeps all DAG states in memory and atomically rewrites one JSON
// file after every mutation. A single server process is assumed (per task
// scope), so an in-process mutex is sufficient; there is no external locking.
type FileStore struct {
	mu   sync.Mutex
	path string
	dags map[string]*dag.DAGState
}

// NewFileStore loads state from path. A missing file means an empty store
// (fresh start); any later empty/zero-byte rewrite is avoided through
// atomic saves, so a zero-byte file is treated as corruption.
func NewFileStore(path string) (*FileStore, error) {
	s := &FileStore{path: path, dags: map[string]*dag.DAGState{}}
	data, err := os.ReadFile(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return s, nil
		}
		return nil, fmt.Errorf("read state file: %w", err)
	}
	if len(data) == 0 {
		return nil, fmt.Errorf("state file %q is empty; refusing to treat as a valid empty state", path)
	}
	var snap snapshot
	if err := json.Unmarshal(data, &snap); err != nil {
		return nil, fmt.Errorf("parse state file: %w", err)
	}
	if snap.Version != version {
		return nil, fmt.Errorf("unsupported state file version %d (want %d)", snap.Version, version)
	}
	s.dags = snap.DAGs
	if s.dags == nil {
		s.dags = map[string]*dag.DAGState{}
	}
	return s, nil
}

// Create inserts a brand-new DAG state and persists.
func (s *FileStore) Create(st *dag.DAGState) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, exists := s.dags[st.ID]; exists {
		return fmt.Errorf("dag %q already exists", st.ID)
	}
	s.dags[st.ID] = st
	return s.saveLocked()
}

// Update applies fn to the DAG state under the store lock and persists the
// result. fn must not retain references to the state after returning.
func (s *FileStore) Update(id string, fn func(*dag.DAGState) error) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	st, ok := s.dags[id]
	if !ok {
		return fmt.Errorf("dag %q not found", id)
	}
	if err := fn(st); err != nil {
		return err
	}
	st.UpdatedAt = time.Now().UTC()
	return s.saveLocked()
}

// Get returns a deep copy of one DAG state.
func (s *FileStore) Get(id string) (*dag.DAGState, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	st, ok := s.dags[id]
	if !ok {
		return nil, ErrNotFound
	}
	return deepCopy(st)
}

// ErrNotFound is returned by Get for unknown ids.
var ErrNotFound = errors.New("dag not found")

// List returns deep copies of all DAG states sorted by creation time.
func (s *FileStore) List() ([]*dag.DAGState, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]*dag.DAGState, 0, len(s.dags))
	for _, st := range s.dags {
		cp, err := deepCopy(st)
		if err != nil {
			return nil, err
		}
		out = append(out, cp)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].CreatedAt.Before(out[j].CreatedAt) })
	return out, nil
}

// saveLocked rewrites the file atomically: write to a temp file in the same
// directory, fsync it, then rename over the target.
func (s *FileStore) saveLocked() error {
	snap := snapshot{Version: version, SavedAt: time.Now().UTC(), DAGs: s.dags}
	data, err := json.MarshalIndent(&snap, "", "  ")
	if err != nil {
		return fmt.Errorf("encode state: %w", err)
	}
	dir := filepath.Dir(s.path)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return fmt.Errorf("create state dir: %w", err)
	}
	tmp, err := os.CreateTemp(dir, ".state-*.json.tmp")
	if err != nil {
		return fmt.Errorf("create temp state file: %w", err)
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName) // no-op after a successful rename
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return fmt.Errorf("write state file: %w", err)
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return fmt.Errorf("fsync state file: %w", err)
	}
	if err := tmp.Close(); err != nil {
		return fmt.Errorf("close state file: %w", err)
	}
	if err := os.Rename(tmpName, s.path); err != nil {
		return fmt.Errorf("replace state file: %w", err)
	}
	return nil
}

func deepCopy(st *dag.DAGState) (*dag.DAGState, error) {
	data, err := json.Marshal(st)
	if err != nil {
		return nil, err
	}
	var cp dag.DAGState
	if err := json.Unmarshal(data, &cp); err != nil {
		return nil, err
	}
	return &cp, nil
}
