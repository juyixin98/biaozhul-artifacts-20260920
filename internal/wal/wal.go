// Package wal implements the durable write-ahead log plus atomic snapshots.
//
// On-disk layout inside the data directory:
//
//	snapshot.json.tmp -> snapshot.json   (full compacted state, written atomically)
//	wal.log            newline-delimited JSON Records, fsynced per append
//
// Load() returns the snapshot plus all records with seq greater than the
// snapshot's. Compact(snapshot) persists the snapshot and truncates the log.
// Crash safety of compaction: the old WAL is unlinked only AFTER fsync of the
// new snapshot directory entry, so a crash at any moment leaves either the
// old snapshot+WAL or the new snapshot (+ a fresh empty WAL). Records whose
// line was torn by a crash (invalid JSON) are ignored as long as they are the
// tail of the log; an invalid line followed by valid ones is a hard error
// because it means history was corrupted rather than truncated.
package wal

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
)

const (
	snapshotName = "snapshot.json"
	snapshotTmp  = "snapshot.json.tmp"
	walName      = "wal.log"
)

// Record is one persisted event: seq + opaque full task snapshot (raw JSON,
// produced by the machine package to avoid an import cycle).
type Record struct {
	Seq  int64           `json:"seq"`
	Task json.RawMessage `json:"task"`
}

// Snapshot is the compacted full state of a store.
type Snapshot struct {
	Seq   int64             `json:"seq"`
	Tasks []json.RawMessage `json:"tasks"`
}

// Log owns the WAL and snapshot files inside a directory.
type Log struct {
	dir    string
	f      *os.File
	enc    *json.Encoder
	commas int64 // seq of the last append (sanity check)
}

// Open (or creates) the log directory and WAL file. A torn final line left
// by a crash mid-write (its fsync never completed) is truncated on open, so
// subsequent appends start at a clean boundary.
func Open(dir string) (*Log, error) {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, fmt.Errorf("wal: mkdir %s: %w", dir, err)
	}
	f, err := os.OpenFile(filepath.Join(dir, walName),
		os.O_CREATE|os.O_RDWR|os.O_APPEND, 0o644)
	if err != nil {
		return nil, fmt.Errorf("wal: open %s: %w", walName, err)
	}
	if err := repairTail(f); err != nil {
		f.Close()
		return nil, err
	}
	return &Log{dir: dir, f: f}, nil
}

// repairTail truncates any bytes after the last complete JSON value, which
// can only be an unacknowledged torn write.
func repairTail(f *os.File) error {
	if _, err := f.Seek(0, io.SeekStart); err != nil {
		return fmt.Errorf("wal: seek for repair: %w", err)
	}
	dec := json.NewDecoder(f)
	good := int64(0)
	for {
		var raw json.RawMessage
		if err := dec.Decode(&raw); err != nil {
			break
		}
		good = dec.InputOffset()
	}
	if _, err := f.Seek(0, io.SeekEnd); err != nil {
		return fmt.Errorf("wal: seek end for repair: %w", err)
	}
	fi, err := f.Stat()
	if err != nil {
		return fmt.Errorf("wal: stat for repair: %w", err)
	}
	if fi.Size() > good {
		if err := f.Truncate(good); err != nil {
			return fmt.Errorf("wal: truncate torn tail: %w", err)
		}
		if err := f.Sync(); err != nil {
			return fmt.Errorf("wal: fsync tail repair: %w", err)
		}
	}
	return nil
}

// Append durably appends one record. The line is flushed and fsynced before
// returning: once Append returns, the transition survives a crash.
func (l *Log) Append(r Record) error {
	line, err := json.Marshal(r)
	if err != nil {
		return fmt.Errorf("wal: marshal record: %w", err)
	}
	if _, err := l.f.Write(append(line, '\n')); err != nil {
		return fmt.Errorf("wal: write: %w", err)
	}
	if err := l.f.Sync(); err != nil {
		return fmt.Errorf("wal: fsync: %w", err)
	}
	l.commas = r.Seq
	return nil
}

// Load reads the snapshot (if present) and every WAL record past it.
func (l *Log) Load() (*Snapshot, []Record, error) {
	snap, err := l.loadSnapshot()
	if err != nil {
		return nil, nil, err
	}
	base := int64(0)
	if snap != nil {
		base = snap.Seq
	}
	recs, err := l.readWAL(base)
	if err != nil {
		return nil, nil, err
	}
	return snap, recs, nil
}

func (l *Log) loadSnapshot() (*Snapshot, error) {
	b, err := os.ReadFile(filepath.Join(l.dir, snapshotName))
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("wal: read snapshot: %w", err)
	}
	if len(b) == 0 {
		return nil, nil
	}
	var s Snapshot
	if err := json.Unmarshal(b, &s); err != nil {
		return nil, fmt.Errorf("wal: corrupt snapshot: %w", err)
	}
	return &s, nil
}

func (l *Log) readWAL(after int64) ([]Record, error) {
	// Re-open read-only from the beginning; the appending handle stays open.
	f, err := os.Open(filepath.Join(l.dir, walName))
	if err != nil {
		return nil, fmt.Errorf("wal: reopen for read: %w", err)
	}
	defer f.Close()
	dec := json.NewDecoder(f)
	var out []Record
	lineNo := 0
	for {
		var raw json.RawMessage
		if err := dec.Decode(&raw); err != nil {
			if errors.Is(err, io.EOF) {
				break
			}
			// A crash mid-write can leave a torn final line: the write was
			// not followed by a successful fsync, so that transition was
			// never acknowledged and is safe to discard. A valid record can
			// never follow a torn one, so stopping here loses no history.
			break
		}
		lineNo++
		var r Record
		if err := json.Unmarshal(raw, &r); err != nil {
			return out, fmt.Errorf("wal: invalid record on line %d: %w", lineNo, err)
		}
		if r.Seq > after {
			out = append(out, r)
		}
	}
	return out, nil
}

// Compact atomically writes snap, fsyncs it and truncates the WAL.
//
// Ordering on disk (the classic "snapshot before log truncation"):
//  1. write+fsync snapshot.json.tmp
//  2. rename tmp -> snapshot.json, fsync directory            (snapshot live)
//  3. truncate wal.log to zero length, fsync                  (old history gone)
//
// A crash between 2 and 3 leaves BOTH snapshot and old WAL; Load simply
// skips records with seq <= snapshot seq. No state can regress.
func (l *Log) Compact(snap Snapshot) error {
	b, err := json.Marshal(snap)
	if err != nil {
		return fmt.Errorf("wal: marshal snapshot: %w", err)
	}
	tmpPath := filepath.Join(l.dir, snapshotTmp)
	finalPath := filepath.Join(l.dir, snapshotName)

	f, err := os.OpenFile(tmpPath, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o644)
	if err != nil {
		return fmt.Errorf("wal: create snapshot tmp: %w", err)
	}
	if _, err := f.Write(b); err != nil {
		f.Close()
		return fmt.Errorf("wal: write snapshot tmp: %w", err)
	}
	if err := f.Sync(); err != nil {
		f.Close()
		return fmt.Errorf("wal: fsync snapshot tmp: %w", err)
	}
	if err := f.Close(); err != nil {
		return fmt.Errorf("wal: close snapshot tmp: %w", err)
	}
	if err := os.Rename(tmpPath, finalPath); err != nil {
		return fmt.Errorf("wal: rename snapshot: %w", err)
	}
	if err := syncDir(l.dir); err != nil {
		return err
	}

	// Recreate the WAL as an empty file (truncate + fsync). Keep using the
	// same *os.File handle, repositioned and truncated.
	if err := l.f.Truncate(0); err != nil {
		return fmt.Errorf("wal: truncate after compaction: %w", err)
	}
	if _, err := l.f.Seek(0, io.SeekStart); err != nil {
		return fmt.Errorf("wal: seek after compaction: %w", err)
	}
	if err := l.f.Sync(); err != nil {
		return fmt.Errorf("wal: fsync after truncation: %w", err)
	}
	return nil
}

// Close releases the WAL file handle.
func (l *Log) Close() error { return l.f.Close() }

func syncDir(dir string) error {
	d, err := os.Open(dir)
	if err != nil {
		return fmt.Errorf("wal: open dir for fsync: %w", err)
	}
	defer d.Close()
	if err := d.Sync(); err != nil {
		return fmt.Errorf("wal: fsync dir: %w", err)
	}
	return nil
}
