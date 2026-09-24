package resource

import (
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
)

type snapshot struct {
	Value     string `json:"value"`
	Version   int64  `json:"version"`
	LastToken int64  `json:"last_token"`
}

func (s *Store) load() error {
	data, err := os.ReadFile(s.path)
	if errors.Is(err, fs.ErrNotExist) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("read resource snapshot: %w", err)
	}
	var snap snapshot
	if err := json.Unmarshal(data, &snap); err != nil {
		return fmt.Errorf("parse resource snapshot: %w", err)
	}
	if snap.LastToken < 0 || snap.Version < 0 {
		return errors.New("invalid resource snapshot: negative token or version")
	}
	s.value = snap.Value
	s.version = snap.Version
	s.lastToken = snap.LastToken
	return nil
}

// persistLocked writes the snapshot atomically. Caller holds s.mu.
func (s *Store) persistLocked() error {
	snap := snapshot{Value: s.value, Version: s.version, LastToken: s.lastToken}
	data, err := json.MarshalIndent(&snap, "", "  ")
	if err != nil {
		return fmt.Errorf("encode resource snapshot: %w", err)
	}

	dir := filepath.Dir(s.path)
	tmp, err := os.CreateTemp(dir, ".res-snap-*")
	if err != nil {
		return fmt.Errorf("create resource snapshot temp file: %w", err)
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName)
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return fmt.Errorf("write resource snapshot: %w", err)
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return fmt.Errorf("fsync resource snapshot: %w", err)
	}
	if err := tmp.Close(); err != nil {
		return fmt.Errorf("close resource snapshot: %w", err)
	}
	if err := os.Rename(tmpName, s.path); err != nil {
		return fmt.Errorf("rename resource snapshot: %w", err)
	}
	return nil
}
