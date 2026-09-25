package cache

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
)

// HistoryRecord stores the last build outcome of one node in one project.
// It is the baseline used to explain why a node rebuilt.
type HistoryRecord struct {
	NodeID     string `json:"node_id"`
	LastKey    string `json:"last_key"`
	LastStatus string `json:"last_status"` // "success" or "failed"
}

// historyFile is the on-disk shape.
type historyFile struct {
	Version int                      `json:"version"`
	Nodes   map[string]HistoryRecord `json:"nodes"`
}

var slugRE = regexp.MustCompile(`[^A-Za-z0-9_.-]+`)

func slug(projectID string) string {
	s := slugRE.ReplaceAllString(projectID, "_")
	if s == "" {
		s = "project"
	}
	return s
}

func (c *Cache) historyPath(projectID string) string {
	return filepath.Join(c.root, "history", slug(projectID)+".json")
}

func (c *Cache) readHistory(projectID string) (*historyFile, error) {
	h := &historyFile{Version: 1, Nodes: map[string]HistoryRecord{}}
	data, err := os.ReadFile(c.historyPath(projectID))
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return h, nil
		}
		return nil, err
	}
	if err := json.Unmarshal(data, h); err != nil {
		return nil, fmt.Errorf("parse history: %w", err)
	}
	if h.Nodes == nil {
		h.Nodes = map[string]HistoryRecord{}
	}
	return h, nil
}

func (c *Cache) writeHistory(projectID string, h *historyFile) error {
	h.Version = 1
	data, err := json.MarshalIndent(h, "", "  ")
	if err != nil {
		return err
	}
	path := c.historyPath(projectID)
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

// HistoryRecord returns the stored record for a node and whether it exists.
func (c *Cache) HistoryRecord(projectID, nodeID string) (HistoryRecord, bool, error) {
	h, err := c.readHistory(projectID)
	if err != nil {
		return HistoryRecord{}, false, err
	}
	rec, ok := h.Nodes[nodeID]
	return rec, ok, nil
}

// SetHistoryRecord upserts one node's record.
func (c *Cache) SetHistoryRecord(projectID string, rec HistoryRecord) error {
	h, err := c.readHistory(projectID)
	if err != nil {
		return err
	}
	h.Nodes[rec.NodeID] = rec
	return c.writeHistory(projectID, h)
}
