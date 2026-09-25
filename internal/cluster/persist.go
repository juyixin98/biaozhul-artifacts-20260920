package cluster

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
)

// Snapshot is the serializable state of a Clusterer.
type Snapshot struct {
	Seq       int64       `json:"seq"`
	NextID    int64       `json:"next_id"`
	Templates []*Template `json:"templates"`
	Evictions []Eviction  `json:"evictions"`
}

// Snapshot captures the current state. The returned value is a deep-enough
// copy for safe serialization (templates are shared but treated read-only).
func (c *Clusterer) Snapshot() *Snapshot {
	return &Snapshot{
		Seq:       c.seq,
		NextID:    c.nextID,
		Templates: c.Templates(),
		Evictions: c.Evictions(),
	}
}

// Restore rebuilds a clusterer from a snapshot. Buckets are reconstructed
// from the templates, so a restored clusterer keeps clustering consistently.
func Restore(cfg Config, s *Snapshot) *Clusterer {
	c := New(cfg)
	c.seq = s.Seq
	c.nextID = s.NextID
	c.evictions = append([]Eviction(nil), s.Evictions...)
	for _, t := range s.Templates {
		key := bucketKey{length: len(t.Tokens), first: t.Tokens[0]}
		c.buckets[key] = append(c.buckets[key], t)
		c.byID[t.ID] = t
		if t.ID >= c.nextID {
			c.nextID = t.ID + 1
		}
	}
	return c
}

// Save writes the snapshot atomically (temp file + rename).
func Save(c *Clusterer, path string) error {
	data, err := json.MarshalIndent(c.Snapshot(), "", "  ")
	if err != nil {
		return fmt.Errorf("marshal snapshot: %w", err)
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o644); err != nil {
		return fmt.Errorf("write snapshot: %w", err)
	}
	if err := os.Rename(tmp, path); err != nil {
		return fmt.Errorf("rename snapshot: %w", err)
	}
	return nil
}

// Load reads a snapshot file. A missing file is not an error: it returns
// (nil, nil) so callers can start fresh.
func Load(path string) (*Snapshot, error) {
	data, err := os.ReadFile(filepath.Clean(path))
	if os.IsNotExist(err) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("read snapshot: %w", err)
	}
	var s Snapshot
	if err := json.Unmarshal(data, &s); err != nil {
		return nil, fmt.Errorf("parse snapshot: %w", err)
	}
	return &s, nil
}
