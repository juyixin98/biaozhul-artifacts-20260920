package store

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
)

// snapshot is the on-disk JSON shape of a store.
type snapshot struct {
	Version int                     `json:"version"`
	Series  map[string]*seriesState `json:"series"`
}

// snapshotVersion is bumped if the persisted shape changes incompatibly.
const snapshotVersion = 1

// Save atomically writes the whole store as a JSON snapshot to path.
//
// The file is written to a temp name in the same directory and renamed into
// place, so a crash mid-write cannot leave a truncated snapshot. This is a
// deliberately simple persistence example for local use, not a durable
// write-ahead log: ingests acknowledged between the last Save and a crash
// are lost.
func (s *Store) Save(path string) (err error) {
	s.mu.RLock()
	snap := snapshot{Version: snapshotVersion, Series: s.series}
	s.mu.RUnlock()

	data, err := json.MarshalIndent(snap, "", "  ")
	if err != nil {
		return fmt.Errorf("encode snapshot: %w", err)
	}

	dir := filepath.Dir(path)
	if dir != "." {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return fmt.Errorf("create data dir: %w", err)
		}
	}
	tmp, err := os.CreateTemp(dir, ".rollup-snap-*.tmp")
	if err != nil {
		return fmt.Errorf("create temp snapshot: %w", err)
	}
	tmpName := tmp.Name()
	defer func() {
		if err != nil {
			_ = os.Remove(tmpName)
		}
	}()
	if _, err = tmp.Write(data); err != nil {
		_ = tmp.Close()
		return fmt.Errorf("write snapshot: %w", err)
	}
	if err = tmp.Sync(); err != nil {
		_ = tmp.Close()
		return fmt.Errorf("fsync snapshot: %w", err)
	}
	if err = tmp.Close(); err != nil {
		return fmt.Errorf("close snapshot: %w", err)
	}
	if err = os.Rename(tmpName, path); err != nil {
		return fmt.Errorf("rename snapshot: %w", err)
	}
	return nil
}

// Load replaces store contents with a snapshot read from path. A missing
// file leaves the store empty and returns no error, so first boot is clean.
func (s *Store) Load(path string) error {
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("read snapshot: %w", err)
	}

	var snap snapshot
	if err := json.Unmarshal(data, &snap); err != nil {
		return fmt.Errorf("decode snapshot: %w", err)
	}
	if snap.Version != snapshotVersion {
		return fmt.Errorf("unsupported snapshot version %d (want %d)", snap.Version, snapshotVersion)
	}

	s.mu.Lock()
	defer s.mu.Unlock()
	if snap.Series == nil {
		snap.Series = map[string]*seriesState{}
	}
	s.series = snap.Series
	return nil
}
