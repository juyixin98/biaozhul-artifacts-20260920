package raft

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
)

// PersistentState is everything that survives a restart.
type PersistentState struct {
	CurrentTerm  int     `json:"currentTerm"`
	VotedFor     int     `json:"votedFor"` // -1 = no vote this term
	Log          []Entry `json:"log"`
	CommitIndex  int     `json:"commitIndex"`
	AppliedIndex int     `json:"appliedIndex"`
}

// Storage abstracts the write-ahead persistence. The lab ships an in-memory
// implementation for the deterministic enumeration and a file-backed one for
// the HTTP demo (both model only the simple "snapshot the whole state"
// durability the tasks require; there is no real fsync strategy).
type Storage interface {
	Load() (PersistentState, error)
	Save(PersistentState) error
}

// ErrEmptyStorage is returned by Load on a never-written storage.
var ErrEmptyStorage = errors.New("raft: empty storage")

// MemoryStorage keeps persistent state in a struct, simulating a disk that
// survives process-level node restarts within one cluster run.
type MemoryStorage struct {
	state PersistentState
	set   bool
}

func NewMemoryStorage() *MemoryStorage { return &MemoryStorage{} }

func (m *MemoryStorage) Load() (PersistentState, error) {
	if !m.set {
		return PersistentState{VotedFor: -1, Log: []Entry{}}, ErrEmptyStorage
	}
	st := m.state
	st.Log = append([]Entry(nil), st.Log...)
	return st, nil
}

func (m *MemoryStorage) Save(st PersistentState) error {
	st.Log = append([]Entry(nil), st.Log...)
	m.state = st
	m.set = true
	return nil
}

// FileStorage persists state as JSON, writing a temp file and renaming it into
// place so a crash never leaves a half-written state file.
type FileStorage struct{ path string }

func NewFileStorage(dir string) *FileStorage {
	return &FileStorage{path: filepath.Join(dir, "raft-state.json")}
}

func (f *FileStorage) Load() (PersistentState, error) {
	var st PersistentState
	b, err := os.ReadFile(f.path)
	if errors.Is(err, os.ErrNotExist) {
		return PersistentState{VotedFor: -1, Log: []Entry{}}, ErrEmptyStorage
	}
	if err != nil {
		return st, err
	}
	if err := json.Unmarshal(b, &st); err != nil {
		return st, err
	}
	if st.Log == nil {
		st.Log = []Entry{}
	}
	return st, nil
}

func (f *FileStorage) Save(st PersistentState) error {
	if err := os.MkdirAll(filepath.Dir(f.path), 0o755); err != nil {
		return err
	}
	b, err := json.MarshalIndent(st, "", "  ")
	if err != nil {
		return err
	}
	tmp := f.path + ".tmp"
	if err := os.WriteFile(tmp, b, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, f.path)
}
