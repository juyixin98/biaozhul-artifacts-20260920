package engine

import (
	"encoding/json"
	"os"
	"path/filepath"
	"time"
)

// Snapshot is the on-disk state format.
type Snapshot struct {
	Version    int                  `json:"version"`
	TZData     string               `json:"tzdataVersion"`
	CatchUp    int                  `json:"catchUpLimit"`
	ExportedAt time.Time            `json:"exportedAt"`
	Schedules  []ScheduleState      `json:"schedules"`
	Fired      map[string]time.Time `json:"firedIds"`
}

// Snapshot returns a serialization copy of the engine state.
func (e *Engine) Snapshot() *Snapshot {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.snapshotLocked()
}

func (e *Engine) snapshotLocked() *Snapshot {
	states := make([]ScheduleState, 0, len(e.schedules))
	for _, st := range e.schedules {
		states = append(states, *cloneState(st))
	}
	fired := make(map[string]time.Time, len(e.fired))
	for k, v := range e.fired {
		fired[k] = v
	}
	return &Snapshot{
		Version:    1,
		TZData:     tzVersion(),
		CatchUp:    e.catchUp,
		ExportedAt: e.clock.Now().UTC(),
		Schedules:  states,
		Fired:      fired,
	}
}

func (e *Engine) persistLocked() {
	if e.store == nil {
		return
	}
	_ = e.store.Save(e.snapshotLocked()) // persistence errors surface via FileStore wrapper logs in main
}

// Restore loads a snapshot into a freshly constructed engine.
func (e *Engine) Restore(snap *Snapshot) {
	e.mu.Lock()
	defer e.mu.Unlock()
	for i := range snap.Schedules {
		st := snap.Schedules[i]
		if st.LastFired != nil {
			t := st.LastFired.UTC()
			st.LastFired = &t
		}
		st.CreatedAt = st.CreatedAt.UTC()
		e.schedules[st.ID] = &st
	}
	for k, v := range snap.Fired {
		e.fired[k] = v
	}
}

// FileStore writes snapshots atomically (temp file + rename).
type FileStore struct {
	path string
}

// NewFileStore targets path (created on first save).
func NewFileStore(path string) *FileStore { return &FileStore{path: path} }

// Save implements Store.
func (s *FileStore) Save(snap *Snapshot) error {
	if err := os.MkdirAll(filepath.Dir(s.path), 0o755); err != nil {
		return err
	}
	b, err := json.MarshalIndent(snap, "", "  ")
	if err != nil {
		return err
	}
	tmp := s.path + ".tmp"
	if err := os.WriteFile(tmp, b, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, s.path)
}

// LoadFile reads a snapshot; a missing file yields (nil, nil).
func LoadFile(path string) (*Snapshot, error) {
	b, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var snap Snapshot
	if err := json.Unmarshal(b, &snap); err != nil {
		return nil, err
	}
	return &snap, nil
}
