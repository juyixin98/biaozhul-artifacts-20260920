package lock

import (
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
)

// load reads the snapshot file. A missing file means "fresh start" and is not
// an error. An expired lease is dropped (the lock is free) but NextToken is
// preserved so tokens never go backwards.
func (s *Store) load() error {
	data, err := os.ReadFile(s.path)
	if errors.Is(err, fs.ErrNotExist) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("read lock snapshot: %w", err)
	}

	var snap snapshot
	if err := json.Unmarshal(data, &snap); err != nil {
		return fmt.Errorf("parse lock snapshot: %w", err)
	}
	if snap.NextToken < 1 {
		return errors.New("invalid snapshot: next_token must be >= 1")
	}
	s.nextToken = snap.NextToken
	s.lease = snap.Lease

	now := s.clk.Now()
	if s.lease != nil && !now.Before(s.lease.Expires) {
		// Lease expired while we were down: release the slot, keep the counter.
		s.lease = nil
		if err := s.persistLocked(); err != nil {
			return err
		}
	}
	return nil
}

// persistLocked writes the snapshot atomically (temp file + rename). Caller
// must hold s.mu.
func (s *Store) persistLocked() error {
	snap := snapshot{
		NextToken: s.nextToken,
		TTLMillis: s.ttl.Milliseconds(),
	}
	if s.lease != nil {
		snap.Lease = s.lease
	}

	data, err := json.MarshalIndent(&snap, "", "  ")
	if err != nil {
		return fmt.Errorf("encode lock snapshot: %w", err)
	}

	dir := filepath.Dir(s.path)
	tmp, err := os.CreateTemp(dir, ".lock-snap-*")
	if err != nil {
		return fmt.Errorf("create lock snapshot temp file: %w", err)
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName)
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return fmt.Errorf("write lock snapshot: %w", err)
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return fmt.Errorf("fsync lock snapshot: %w", err)
	}
	if err := tmp.Close(); err != nil {
		return fmt.Errorf("close lock snapshot: %w", err)
	}
	if err := os.Rename(tmpName, s.path); err != nil {
		return fmt.Errorf("rename lock snapshot: %w", err)
	}
	return nil
}
