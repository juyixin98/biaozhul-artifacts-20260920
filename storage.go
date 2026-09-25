package tailsampling

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sync"
)

// Store is the local persistence boundary: an append-only decision log,
// an append-only late-arrival log and a periodically rewritten snapshot of
// still-open traces. All writes are durable at syscall boundaries (no
// fsync per record; Close fsyncs via file Close).
type Store interface {
	AppendDecision(d Decision) error
	AppendLate(e LateEvent) error
	LoadDecisions() ([]Decision, error)
	LoadLate() ([]LateEvent, error)
	WriteSnapshot(st Snapshot) error
	ReadSnapshot() (*Snapshot, bool, error)
	Close() error
}

// Snapshot is persisted periodically and reloaded on restart so that open
// traces survive a process bounce.
type Snapshot struct {
	TakenAtMs int64   `json:"taken_at_ms"`
	Open      []Trace `json:"open"`
}

// FileStore implements Store with plain files under DataDir:
//
//	decisions.jsonl, late_arrivals.jsonl, snapshot.json
type FileStore struct {
	dir string

	mu       sync.Mutex
	decFile  *os.File
	lateFile *os.File
	decW     *bufio.Writer
	lateW    *bufio.Writer
}

func OpenFileStore(dir string) (*FileStore, error) {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, fmt.Errorf("create data dir: %w", err)
	}
	df, err := os.OpenFile(filepath.Join(dir, "decisions.jsonl"),
		os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return nil, err
	}
	lf, err := os.OpenFile(filepath.Join(dir, "late_arrivals.jsonl"),
		os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		_ = df.Close()
		return nil, err
	}
	return &FileStore{
		dir:      dir,
		decFile:  df,
		lateFile: lf,
		decW:     bufio.NewWriter(df),
		lateW:    bufio.NewWriter(lf),
	}, nil
}

func (s *FileStore) AppendDecision(d Decision) error {
	return s.append(s.decW, s.decFile, d)
}

func (s *FileStore) AppendLate(e LateEvent) error {
	return s.append(s.lateW, s.lateFile, e)
}

func (s *FileStore) append(w *bufio.Writer, f *os.File, v any) error {
	b, err := json.Marshal(v)
	if err != nil {
		return err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, err := w.Write(b); err != nil {
		return err
	}
	if err := w.WriteByte('\n'); err != nil {
		return err
	}
	if err := w.Flush(); err != nil {
		return err
	}
	return f.Sync()
}

func readJSONL[T any](path string) ([]T, error) {
	f, err := os.Open(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}
	defer f.Close()
	var out []T
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 0, 64*1024), 16*1024*1024)
	for sc.Scan() {
		line := sc.Bytes()
		if len(line) == 0 {
			continue
		}
		var v T
		if err := json.Unmarshal(line, &v); err != nil {
			return nil, fmt.Errorf("corrupt record in %s: %w", path, err)
		}
		out = append(out, v)
	}
	return out, sc.Err()
}

func (s *FileStore) LoadDecisions() ([]Decision, error) {
	return readJSONL[Decision](filepath.Join(s.dir, "decisions.jsonl"))
}

func (s *FileStore) LoadLate() ([]LateEvent, error) {
	return readJSONL[LateEvent](filepath.Join(s.dir, "late_arrivals.jsonl"))
}

func (s *FileStore) snapshotPath() string { return filepath.Join(s.dir, "snapshot.json") }

// WriteSnapshot replaces snapshot.json atomically (temp file + rename).
func (s *FileStore) WriteSnapshot(st Snapshot) error {
	b, err := json.MarshalIndent(st, "", "  ")
	if err != nil {
		return err
	}
	tmp := s.snapshotPath() + ".tmp"
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := os.WriteFile(tmp, b, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, s.snapshotPath())
}

func (s *FileStore) ReadSnapshot() (*Snapshot, bool, error) {
	b, err := os.ReadFile(s.snapshotPath())
	if err != nil {
		if os.IsNotExist(err) {
			return nil, false, nil
		}
		return nil, false, err
	}
	var st Snapshot
	if err := json.Unmarshal(b, &st); err != nil {
		return nil, false, err
	}
	return &st, true, nil
}

func (s *FileStore) Close() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	var errs []error
	if err := s.decW.Flush(); err != nil {
		errs = append(errs, err)
	}
	if err := s.lateW.Flush(); err != nil {
		errs = append(errs, err)
	}
	if err := s.decFile.Close(); err != nil {
		errs = append(errs, err)
	}
	if err := s.lateFile.Close(); err != nil {
		errs = append(errs, err)
	}
	return errors.Join(errs...)
}
