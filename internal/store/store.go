// Package store is the file-backed, append-only event store.
//
// Layout under the cache root:
//
//	<cacheDir>/runs/<runID>/events.jsonl
//	<cacheDir>/runs/<runID>/executions.jsonl
//
// The cache root is separate from the executor work directory: deleting a
// work directory never touches recorded events and vice versa.
package store

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/local/testmerge/internal/domain"
)

var runIDRe = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$`)

// ValidRunID checks run ids so they can never escape the cache directory.
func ValidRunID(id string) bool { return runIDRe.MatchString(id) }

var (
	// ErrNotFound is returned when a run directory does not exist.
	ErrNotFound = errors.New("run not found")
	// ErrExists is returned when creating a run that already exists.
	ErrExists = errors.New("run already exists")
)

// DuplicateEventError marks an Append whose EventID is already recorded.
type DuplicateEventError struct{ EventID string }

func (e *DuplicateEventError) Error() string {
	return fmt.Sprintf("duplicate event_id %s (replay ignored)", e.EventID)
}

// SeqCollisionError marks two different events sharing one seq_no.
type SeqCollisionError struct {
	SeqNo int64
	A, B  string
}

func (e *SeqCollisionError) Error() string {
	return fmt.Sprintf("seq_no %d used by both events %s and %s", e.SeqNo, e.A, e.B)
}

type runCache struct {
	loaded bool
	events []domain.Event
	ids    map[string]struct{}
	seqs   map[int64]string // seq_no -> event id (non run_started)
}

// Store manages JSONL logs under a cache directory.
type Store struct {
	root string
	mu   sync.Mutex
	runs map[string]*runCache
}

// Open (or creates) a store at cacheDir.
func Open(cacheDir string) (*Store, error) {
	if err := os.MkdirAll(filepath.Join(cacheDir, "runs"), 0o755); err != nil {
		return nil, fmt.Errorf("create cache dir: %w", err)
	}
	abs, err := filepath.Abs(cacheDir)
	if err != nil {
		return nil, err
	}
	return &Store{root: abs, runs: map[string]*runCache{}}, nil
}

// Root returns the absolute cache root.
func (s *Store) Root() string { return s.root }

func (s *Store) runDir(runID string) string {
	return filepath.Join(s.root, "runs", runID)
}

func (s *Store) eventsPath(runID string) string {
	return filepath.Join(s.runDir(runID), "events.jsonl")
}

func (s *Store) execPath(runID string) string {
	return filepath.Join(s.runDir(runID), "executions.jsonl")
}

// CreateRun creates a new, empty run directory.
func (s *Store) CreateRun(runID string) error {
	if !ValidRunID(runID) {
		return fmt.Errorf("invalid run id %q", runID)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	dir := s.runDir(runID)
	if _, err := os.Stat(dir); err == nil {
		return ErrExists
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return fmt.Errorf("create run dir: %w", err)
	}
	s.runs[runID] = &runCache{loaded: true, ids: map[string]struct{}{}, seqs: map[int64]string{}}
	return nil
}

// ListRuns returns run ids sorted by name.
func (s *Store) ListRuns() ([]string, error) {
	entries, err := os.ReadDir(filepath.Join(s.root, "runs"))
	if err != nil {
		return nil, err
	}
	var out []string
	for _, en := range entries {
		if en.IsDir() {
			out = append(out, en.Name())
		}
	}
	sort.Strings(out)
	return out, nil
}

func (s *Store) cacheFor(runID string) *runCache {
	c := s.runs[runID]
	if c == nil {
		c = &runCache{}
		s.runs[runID] = c
	}
	return c
}

// loadLocked reads the JSONL log into memory. Caller must hold s.mu.
func (s *Store) loadLocked(runID string) (*runCache, error) {
	if _, err := os.Stat(s.runDir(runID)); errors.Is(err, os.ErrNotExist) {
		return nil, ErrNotFound
	}
	c := s.cacheFor(runID)
	if c.loaded {
		return c, nil
	}
	c.ids = map[string]struct{}{}
	c.seqs = map[int64]string{}

	f, err := os.Open(s.eventsPath(runID))
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			c.loaded = true
			return c, nil
		}
		return nil, err
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 64*1024), 16*1024*1024)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" {
			continue
		}
		var e domain.Event
		if err := json.Unmarshal([]byte(line), &e); err != nil {
			return nil, fmt.Errorf("corrupt event log for run %s: %w", runID, err)
		}
		c.events = append(c.events, e)
		c.ids[e.EventID] = struct{}{}
		if e.Type != domain.EventRunStarted {
			c.seqs[e.SeqNo] = e.EventID
		}
	}
	if err := sc.Err(); err != nil {
		return nil, err
	}
	c.loaded = true
	return c, nil
}

// AppendRunFinished atomically appends a synthesized run_finished event,
// choosing seq_no = max(existing seq)+1. A duplicate eventID (e.g. the same
// execution retried) is ignored. Used for the "auto_finish" convenience.
func (s *Store) AppendRunFinished(runID, eventID string, cancelled bool, status domain.RunStatus, message string) (domain.Event, error) {
	if !ValidRunID(runID) {
		return domain.Event{}, fmt.Errorf("invalid run id %q", runID)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	c, err := s.loadLocked(runID)
	if err != nil {
		return domain.Event{}, err
	}
	if _, ok := c.ids[eventID]; ok {
		return domain.Event{}, &DuplicateEventError{EventID: eventID}
	}
	var max int64
	for _, e := range c.events {
		if e.SeqNo > max {
			max = e.SeqNo
		}
	}
	ev := domain.Event{
		EventID:   eventID,
		SeqNo:     max + 1,
		Type:      domain.EventRunFinished,
		RunID:     runID,
		Status:    string(status),
		Cancelled: cancelled,
		At:        time.Now().UTC().Format(time.RFC3339Nano),
		Message:   message,
	}
	line, err := json.Marshal(ev)
	if err != nil {
		return domain.Event{}, err
	}
	f, err := os.OpenFile(s.eventsPath(runID), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return domain.Event{}, err
	}
	if _, err := f.Write(append(line, '\n')); err != nil {
		f.Close()
		return domain.Event{}, err
	}
	if err := f.Sync(); err != nil {
		f.Close()
		return domain.Event{}, err
	}
	if err := f.Close(); err != nil {
		return domain.Event{}, err
	}
	c.events = append(c.events, ev)
	c.ids[ev.EventID] = struct{}{}
	c.seqs[ev.SeqNo] = ev.EventID
	return ev, nil
}

// AppendEvent validates, dedupes and durably appends one event. A duplicate
// EventID returns *DuplicateEventError (callers treat it as idempotent).
func (s *Store) AppendEvent(e domain.Event) error {
	if !ValidRunID(e.RunID) {
		return fmt.Errorf("invalid run id %q", e.RunID)
	}
	s.mu.Lock()
	defer s.mu.Unlock()

	c, err := s.loadLocked(e.RunID)
	if err != nil {
		return err
	}
	if _, ok := c.ids[e.EventID]; ok {
		return &DuplicateEventError{EventID: e.EventID}
	}
	if e.Type != domain.EventRunStarted {
		if other, clash := c.seqs[e.SeqNo]; clash {
			return &SeqCollisionError{SeqNo: e.SeqNo, A: other, B: e.EventID}
		}
	}

	line, err := json.Marshal(e)
	if err != nil {
		return err
	}
	f, err := os.OpenFile(s.eventsPath(e.RunID), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return err
	}
	_, werr := f.Write(append(line, '\n'))
	syncerr := f.Sync()
	closeErr := f.Close()
	if werr != nil {
		return werr
	}
	if syncerr != nil {
		return syncerr
	}
	if closeErr != nil {
		return closeErr
	}

	c.events = append(c.events, e)
	c.ids[e.EventID] = struct{}{}
	if e.Type != domain.EventRunStarted {
		c.seqs[e.SeqNo] = e.EventID
	}
	return nil
}

// LoadEvents returns all recorded events for a run (in log order; reducers
// canonicalize order themselves).
func (s *Store) LoadEvents(runID string) ([]domain.Event, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	c, err := s.loadLocked(runID)
	if err != nil {
		return nil, err
	}
	out := make([]domain.Event, len(c.events))
	copy(out, c.events)
	return out, nil
}

// MaxSeq returns the highest seq_no currently recorded for a run.
func (s *Store) MaxSeq(runID string) (int64, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	c, err := s.loadLocked(runID)
	if err != nil {
		return 0, err
	}
	var max int64
	for _, e := range c.events {
		if e.SeqNo > max {
			max = e.SeqNo
		}
	}
	return max, nil
}

// AppendExecution appends one audit record of a fixture execution.
func (s *Store) AppendExecution(runID string, rec any) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, err := os.Stat(s.runDir(runID)); errors.Is(err, os.ErrNotExist) {
		return ErrNotFound
	}
	line, err := json.Marshal(rec)
	if err != nil {
		return err
	}
	f, err := os.OpenFile(s.execPath(runID), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return err
	}
	defer f.Close()
	if _, err := f.Write(append(line, '\n')); err != nil {
		return err
	}
	return f.Sync()
}

// LoadExecutions reads raw JSON audit records for a run.
func (s *Store) LoadExecutions(runID string) ([]json.RawMessage, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, err := os.Stat(s.runDir(runID)); errors.Is(err, os.ErrNotExist) {
		return nil, ErrNotFound
	}
	f, err := os.Open(s.execPath(runID))
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, nil // run exists but nothing executed yet
		}
		return nil, err
	}
	defer f.Close()
	var out []json.RawMessage
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 64*1024), 16*1024*1024)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line != "" {
			out = append(out, json.RawMessage(line))
		}
	}
	return out, sc.Err()
}
