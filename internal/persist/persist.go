// Package persist stores and loads store snapshots as JSON files with an
// atomic write: data is written to a temp file, fsynced, and renamed over
// the target, so a crash never leaves a torn snapshot.
package persist

import (
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"

	"cardinalitybudget/internal/store"
)

// Store handles snapshot files in one directory.
type Store struct {
	path string
}

const snapshotName = "snapshot.json"

// New creates the data directory if needed.
func New(dir string) (*Store, error) {
	if dir == "" {
		return nil, fmt.Errorf("persist directory must not be empty")
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, fmt.Errorf("create data dir %q: %w", dir, err)
	}
	return &Store{path: filepath.Join(dir, snapshotName)}, nil
}

// Path returns the snapshot file location.
func (p *Store) Path() string { return p.path }

// Exists reports whether a snapshot file is present.
func (p *Store) Exists() (bool, error) {
	if _, err := os.Stat(p.path); err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return false, nil
		}
		return false, err
	}
	return true, nil
}

// Save atomically writes a snapshot.
func (p *Store) Save(snap store.Snapshot) error {
	data, err := json.MarshalIndent(snap, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal snapshot: %w", err)
	}
	data = append(data, '\n')

	dir := filepath.Dir(p.path)
	tmp, err := os.CreateTemp(dir, ".snapshot-*.tmp")
	if err != nil {
		return fmt.Errorf("create temp snapshot: %w", err)
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName) // no-op after a successful rename

	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return fmt.Errorf("write temp snapshot: %w", err)
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return fmt.Errorf("fsync temp snapshot: %w", err)
	}
	if err := tmp.Close(); err != nil {
		return fmt.Errorf("close temp snapshot: %w", err)
	}
	if err := os.Rename(tmpName, p.path); err != nil {
		return fmt.Errorf("rename snapshot: %w", err)
	}
	if dirF, err := os.Open(dir); err == nil {
		_ = dirF.Sync() // best-effort durability of the rename
		dirF.Close()
	}
	return nil
}

// Load reads and parses a snapshot. A missing file returns fs.ErrNotExist;
// use Exists first if that case matters.
func (p *Store) Load() (store.Snapshot, error) {
	var snap store.Snapshot
	data, err := os.ReadFile(p.path)
	if err != nil {
		return snap, err
	}
	if err := json.Unmarshal(data, &snap); err != nil {
		return snap, fmt.Errorf("parse snapshot %q: %w", p.path, err)
	}
	if snap.Version != store.SnapshotVersion {
		return snap, fmt.Errorf("unsupported snapshot version %d (want %d)", snap.Version, store.SnapshotVersion)
	}
	return snap, nil
}
