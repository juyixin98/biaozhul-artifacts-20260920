package engine

import (
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"

	"logcluster/internal/cluster"
	"logcluster/internal/ring"
)

// SnapshotFormatVersion is bumped when the on-disk shape changes incompatibly.
const SnapshotFormatVersion = 1

// snapshot is the on-disk representation. Slices are stored in deterministic
// order (clusters and events oldest-first) so identical state serializes
// byte-identically.
type snapshot struct {
	FormatVersion  int                `json:"format_version"`
	SavedAt        string             `json:"saved_at"`
	NextClusterID  int                `json:"next_cluster_id"`
	TotalLines     int64              `json:"total_lines"`
	TruncatedLines int64              `json:"truncated_lines"`
	EvictedCount   int64              `json:"evicted_count"`
	Evicted        []EvictedEntry     `json:"evicted"`
	Clusters       []*cluster.Cluster `json:"clusters"`
	Events         []ring.Event       `json:"events"`
}

// Save writes a deterministic JSON snapshot to path atomically (tmp + rename).
func (e *Engine) Save(path string) error {
	e.mu.Lock()
	ids := sortedKeys(e.clusters)
	clusters := make([]*cluster.Cluster, 0, len(ids))
	for _, id := range ids {
		clusters = append(clusters, e.clusters[id])
	}
	snap := snapshot{
		FormatVersion:  SnapshotFormatVersion,
		SavedAt:        e.clock.Now().Format("2006-01-02T15:04:05.000000000Z07:00"),
		NextClusterID:  e.nextID,
		TotalLines:     e.totalLines,
		TruncatedLines: e.truncated,
		EvictedCount:   e.evictedN,
		Evicted:        append([]EvictedEntry(nil), e.evicted...),
		Clusters:       clusters,
		Events:         e.events.Snapshot(),
	}
	e.mu.Unlock()

	data, err := json.MarshalIndent(snap, "", "  ")
	if err != nil {
		return err
	}
	data = append(data, '\n')

	dir := filepath.Dir(path)
	tmp, err := os.CreateTemp(dir, ".logcluster-snap-*")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName)
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(tmpName, path)
}

// Load restores a snapshot written by Save. A missing file is reported as-is;
// callers may treat fs.ErrNotExist as "fresh start".
func (e *Engine) Load(path string) error {
	data, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	var snap snapshot
	if err := json.Unmarshal(data, &snap); err != nil {
		return fmt.Errorf("parse snapshot: %w", err)
	}
	if snap.FormatVersion != SnapshotFormatVersion {
		return fmt.Errorf("unsupported snapshot format version %d (want %d)", snap.FormatVersion, SnapshotFormatVersion)
	}

	e.mu.Lock()
	defer e.mu.Unlock()

	e.clusters = make(map[int]*cluster.Cluster, len(snap.Clusters))
	for _, c := range snap.Clusters {
		c.Rehydrate()
		e.clusters[c.ID] = c
	}
	// Guard against corrupt or hand-edited files.
	if snap.NextClusterID > e.nextID {
		e.nextID = snap.NextClusterID
	}
	for _, c := range snap.Clusters {
		if c.ID >= e.nextID {
			e.nextID = c.ID + 1
		}
	}
	e.totalLines = snap.TotalLines
	e.truncated = snap.TruncatedLines
	e.evictedN = snap.EvictedCount
	e.evicted = append([]EvictedEntry(nil), snap.Evicted...)
	e.events.Restore(snap.Events)
	return nil
}

// LoadIfExists restores path when present and returns true; a missing file
// leaves the engine untouched and returns (false, nil).
func (e *Engine) LoadIfExists(path string) (bool, error) {
	if err := e.Load(path); err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return false, nil
		}
		return false, err
	}
	return true, nil
}
