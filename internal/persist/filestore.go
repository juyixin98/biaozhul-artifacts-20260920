// Package persist provides local JSON-file persistence for the engine
// snapshot. Writes are atomic (temp file + fsync + rename) so a crash never
// leaves a torn snapshot behind.
package persist

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"

	"github.com/example/hysteresis-alerter/internal/engine"
)

// FileStore persists snapshots to a single JSON file.
type FileStore struct {
	path string
}

// New creates a FileStore rooted at path (parent directories are created).
func New(path string) (*FileStore, error) {
	if path == "" {
		return nil, fmt.Errorf("persist path is empty")
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return nil, fmt.Errorf("create persist directory: %w", err)
	}
	return &FileStore{path: path}, nil
}

// Path returns the snapshot file location.
func (s *FileStore) Path() string { return s.path }

// Save atomically writes snap.
func (s *FileStore) Save(snap *engine.Snapshot) error {
	data, err := json.MarshalIndent(snap, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal snapshot: %w", err)
	}
	dir := filepath.Dir(s.path)
	tmp, err := os.CreateTemp(dir, ".snapshot-*.tmp")
	if err != nil {
		return fmt.Errorf("create temp snapshot: %w", err)
	}
	tmpName := tmp.Name()
	cleanup := func() { _ = os.Remove(tmpName) }

	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		cleanup()
		return fmt.Errorf("write snapshot: %w", err)
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		cleanup()
		return fmt.Errorf("fsync snapshot: %w", err)
	}
	if err := tmp.Close(); err != nil {
		cleanup()
		return fmt.Errorf("close snapshot: %w", err)
	}
	if err := os.Rename(tmpName, s.path); err != nil {
		cleanup()
		return fmt.Errorf("rename snapshot: %w", err)
	}
	return nil
}

// Load reads the snapshot. A missing file is reported as (nil, nil) so
// callers can start a fresh engine.
func (s *FileStore) Load() (*engine.Snapshot, error) {
	data, err := os.ReadFile(s.path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, fmt.Errorf("read snapshot: %w", err)
	}
	var snap engine.Snapshot
	if err := json.Unmarshal(data, &snap); err != nil {
		return nil, fmt.Errorf("parse snapshot: %w", err)
	}
	return &snap, nil
}
