package sampler

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sync"
)

// Store persists decisions and kept spans as JSONL files so that a restart
// keeps decision consistency (late spans still get the same verdict).
// A nil *Store is a valid no-store: all methods are no-ops.
type Store struct {
	mu  sync.Mutex
	dir string
	dec *os.File
	spn *os.File
}

// NewStore opens (creating if needed) the persistence files in dir.
// An empty dir returns the no-op store.
func NewStore(dir string) (*Store, error) {
	if dir == "" {
		return nil, nil
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, err
	}
	dec, err := os.OpenFile(filepath.Join(dir, "decisions.jsonl"), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		return nil, err
	}
	spn, err := os.OpenFile(filepath.Join(dir, "spans.jsonl"), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		dec.Close()
		return nil, err
	}
	return &Store{dir: dir, dec: dec, spn: spn}, nil
}

// AppendDecision persists one decision record.
func (s *Store) AppendDecision(d Decision) error {
	if s == nil {
		return nil
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	return writeJSONL(s.dec, d)
}

// AppendSpans persists kept spans (including late spans of kept traces).
func (s *Store) AppendSpans(spans []Span) error {
	if s == nil {
		return nil
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, sp := range spans {
		if err := writeJSONL(s.spn, sp); err != nil {
			return err
		}
	}
	return nil
}

// LoadDecisions reads every persisted decision, newest record wins per trace.
func LoadDecisions(dir string) (map[string]Decision, error) {
	out := map[string]Decision{}
	if dir == "" {
		return out, nil
	}
	f, err := os.Open(filepath.Join(dir, "decisions.jsonl"))
	if os.IsNotExist(err) {
		return out, nil
	}
	if err != nil {
		return nil, err
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 0, 1<<20), 1<<20)
	for sc.Scan() {
		line := sc.Bytes()
		if len(line) == 0 {
			continue
		}
		var d Decision
		if err := json.Unmarshal(line, &d); err != nil {
			return nil, fmt.Errorf("decisions.jsonl: %w", err)
		}
		out[d.TraceID] = d
	}
	return out, sc.Err()
}

// Close flushes and closes the store files.
func (s *Store) Close() {
	if s == nil {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.dec.Close()
	s.spn.Close()
}

func writeJSONL(f *os.File, v any) error {
	b, err := json.Marshal(v)
	if err != nil {
		return err
	}
	if _, err := f.Write(append(b, '\n')); err != nil {
		return err
	}
	return f.Sync()
}
