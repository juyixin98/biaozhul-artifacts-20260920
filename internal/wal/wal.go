// Package wal implements a tiny fsync'd append-only write-ahead log.
//
// Each record is one JSON object followed by '\n'. A record is considered
// durable only after Write has returned (the write and fsync are both done).
// On open, a torn final record left by a process killed mid-write is removed
// by truncating the file back to the end of the last complete newline.
package wal

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
)

// WAL is an append-only, fsync'd JSON-line log.
type WAL struct {
	f    *os.File
	path string
}

// Open creates datadir if needed and opens (or creates) datadir/wal.log.
// A trailing partial record (no terminating newline) is truncated away,
// modelling a crash between the write and the fsync completing.
func Open(datadir string) (*WAL, error) {
	if err := os.MkdirAll(datadir, 0o755); err != nil {
		return nil, fmt.Errorf("wal: mkdir %s: %w", datadir, err)
	}
	path := filepath.Join(datadir, "wal.log")
	f, err := os.OpenFile(path, os.O_RDWR|os.O_CREATE, 0o644)
	if err != nil {
		return nil, fmt.Errorf("wal: open %s: %w", path, err)
	}
	if err := repairTail(f); err != nil {
		f.Close()
		return nil, err
	}
	return &WAL{f: f, path: path}, nil
}

// repairTail truncates any bytes after the final '\n' so a torn write
// cannot poison replay. An empty file is left untouched.
func repairTail(f *os.File) error {
	info, err := f.Stat()
	if err != nil {
		return err
	}
	size := info.Size()
	if size == 0 {
		return nil
	}
	// Scan backwards for the last newline. Read in blocks from the tail;
	// a record can be arbitrarily large (bounded only by disk), so the scan
	// must not give up after a fixed window.
	bufSize := int64(8192)
	buf := make([]byte, bufSize)
	var lastNL int64 = -1
	for pos := size; pos > 0; {
		start := pos - bufSize
		if start < 0 {
			start = 0
		}
		n, err := f.ReadAt(buf[:pos-start], start)
		if err != nil && !(err == io.EOF && n > 0) {
			return err
		}
		for i := n - 1; i >= 0; i-- {
			if buf[i] == '\n' {
				lastNL = start + int64(i)
				goto found
			}
		}
		pos = start
	}
found:
	if lastNL < 0 {
		// No newline anywhere: the whole file is one torn record.
		return f.Truncate(0)
	}
	if cut := lastNL + 1; cut < size {
		return f.Truncate(cut)
	}
	return nil
}

// Append marshals rec as JSON, writes it with a trailing newline and fsyncs
// before returning. Callers can rely on the crash points that fire here to
// distinguish "before" and "after" durability exactly.
func (w *WAL) Append(rec any) error {
	line, err := json.Marshal(rec)
	if err != nil {
		return fmt.Errorf("wal: marshal: %w", err)
	}
	line = append(line, '\n')
	if _, err := w.f.Write(line); err != nil {
		return fmt.Errorf("wal: write: %w", err)
	}
	if err := w.f.Sync(); err != nil {
		return fmt.Errorf("wal: sync: %w", err)
	}
	return nil
}

// Replay decodes every complete record into rec and calls handle. Decoding is
// done into the caller-provided record so that concrete struct types (with
// custom UnmarshalJSON if needed) stay in this package's caller.
func (w *WAL) Replay(rec any, handle func() error) error {
	if _, err := w.f.Seek(0, io.SeekStart); err != nil {
		return err
	}
	defer w.f.Seek(0, io.SeekEnd)
	sc := bufio.NewScanner(w.f)
	sc.Buffer(make([]byte, 0, 64*1024), 4*1024*1024)
	for sc.Scan() {
		line := bytes.TrimSpace(sc.Bytes())
		if len(line) == 0 {
			continue
		}
		if err := json.Unmarshal(line, rec); err != nil {
			return fmt.Errorf("wal: replay decode: %w", err)
		}
		if err := handle(); err != nil {
			return err
		}
	}
	return sc.Err()
}

// Close flushes nothing extra (Append already fsyncs) and closes the file.
func (w *WAL) Close() error { return w.f.Close() }

// Path returns the path of the log file.
func (w *WAL) Path() string { return w.path }
