// Package storage provides the local persistence sample: a crash-safe
// append-only WAL plus per-trace JSON snapshots. It is deliberately simple —
// no external database — to demonstrate the persistence seam.
package storage

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"sync"

	"tracestitch/internal/assembler"
	"tracestitch/internal/model"
)

// FileStore writes data/wal.jsonl and data/snapshots/<traceId>.json.
type FileStore struct {
	mu      sync.Mutex
	dir     string
	wal     *os.File
	writer  *bufio.Writer
	encoder *json.Encoder
}

const (
	walDirName   = "data"
	walFileName  = "wal.jsonl"
	snapshotsDir = "snapshots"
)

// Open (or create) the store under baseDir. The WAL is opened O_APPEND so a
// restart resumes where it stopped; every record is flushed+fsynced.
func Open(baseDir string) (*FileStore, error) {
	dataDir := filepath.Join(baseDir, walDirName)
	if err := os.MkdirAll(filepath.Join(dataDir, snapshotsDir), 0o755); err != nil {
		return nil, fmt.Errorf("create data dir: %w", err)
	}
	walPath := filepath.Join(dataDir, walFileName)
	f, err := os.OpenFile(walPath, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return nil, fmt.Errorf("open wal: %w", err)
	}
	w := bufio.NewWriter(f)
	return &FileStore{
		dir:     dataDir,
		wal:     f,
		writer:  w,
		encoder: json.NewEncoder(w),
	}, nil
}

// Append writes one record and fsyncs it before returning, so a crash cannot
// lose an accepted span that the API already acknowledged.
func (s *FileStore) Append(rec assembler.WALRecord) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.encoder.Encode(rec); err != nil {
		return fmt.Errorf("wal encode: %w", err)
	}
	if err := s.writer.Flush(); err != nil {
		return fmt.Errorf("wal flush: %w", err)
	}
	if err := s.wal.Sync(); err != nil {
		return fmt.Errorf("wal sync: %w", err)
	}
	return nil
}

// SaveSnapshot atomically replaces the trace's snapshot file
// (write to tmp + rename, so readers never see a half-written file).
func (s *FileStore) SaveSnapshot(traceID string, view model.TraceView) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	final := s.snapshotPath(traceID)
	tmp := final + ".tmp"
	data, err := json.MarshalIndent(view, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal snapshot: %w", err)
	}
	if err := os.WriteFile(tmp, data, 0o644); err != nil {
		return fmt.Errorf("write snapshot: %w", err)
	}
	if err := os.Rename(tmp, final); err != nil {
		return fmt.Errorf("rename snapshot: %w", err)
	}
	return nil
}

func (s *FileStore) snapshotPath(traceID string) string {
	return filepath.Join(s.dir, snapshotsDir, traceID+".json")
}

// Close flushes and closes the WAL.
func (s *FileStore) Close() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.writer.Flush(); err != nil {
		s.wal.Close()
		return err
	}
	return s.wal.Close()
}

// ReadWAL returns all records in append order.
func (s *FileStore) ReadWAL() ([]assembler.WALRecord, error) {
	path := filepath.Join(s.dir, walFileName)
	f, err := os.Open(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}
	defer f.Close()

	var recs []assembler.WALRecord
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 1024*1024), 16*1024*1024)
	line := 0
	for sc.Scan() {
		line++
		raw := sc.Bytes()
		if len(raw) == 0 {
			continue
		}
		var rec assembler.WALRecord
		if err := json.Unmarshal(raw, &rec); err != nil {
			return nil, fmt.Errorf("wal line %d: %w", line, err)
		}
		recs = append(recs, rec)
	}
	if err := sc.Err(); err != nil {
		return nil, fmt.Errorf("read wal: %w", err)
	}
	return recs, nil
}

// SnapshotFiles returns snapshot file names (sorted), used by the demo docs
// to point at local persistence artifacts.
func (s *FileStore) SnapshotFiles() ([]string, error) {
	entries, err := os.ReadDir(filepath.Join(s.dir, snapshotsDir))
	if err != nil {
		return nil, err
	}
	var names []string
	for _, e := range entries {
		if !e.IsDir() && filepath.Ext(e.Name()) == ".json" {
			names = append(names, e.Name())
		}
	}
	sort.Strings(names)
	return names, nil
}
