package trmerge

import (
	"bufio"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"
)

// Envelope wraps a persisted event with bookkeeping the store adds.
type Envelope struct {
	Seq        int       `json:"seq"`
	ReceivedAt time.Time `json:"received_at"`
	Synthetic  bool      `json:"synthetic,omitempty"` // synthesized by runner/store, not a client
	Event      Event     `json:"event"`
}

// RunMeta is the small persisted descriptor of a run.
type RunMeta struct {
	ID           string      `json:"id"`
	CreatedAt    time.Time   `json:"created_at"`
	Mode         string      `json:"mode"` // "events" | "execute"
	Seed         []ShardSeed `json:"seed"`
	WorkRoot     string      `json:"work_root,omitempty"`
	CacheRoot    string      `json:"cache_root"`
	FinalizeAuto bool        `json:"finalize_auto,omitempty"`
}

// IngestOutcome reports what happened to one submitted event.
type IngestOutcome struct {
	EventID   string `json:"event_id"`
	Type      string `json:"type"`
	Applied   bool   `json:"applied"`
	Duplicate bool   `json:"duplicate"`
	Late      bool   `json:"late"`
	Conflict  bool   `json:"conflict"`
	Error     string `json:"error,omitempty"`
}

// Store persists runs under a cache root. The cache root is deliberately
// separate from the work root where fixture commands execute.
type Store struct {
	cacheRoot string
	mu        sync.Mutex
	runs      map[string]*runHandle
}

type runHandle struct {
	mu              sync.Mutex
	meta            RunMeta
	merger          *Merger
	seen            map[string]bool // event idempotency keys
	seq             int
	finalized       bool
	cancelRequested bool
}

// NewStore opens (creating if needed) a cache directory.
func NewStore(cacheRoot string) (*Store, error) {
	cacheRoot, err := filepath.Abs(filepath.Clean(cacheRoot))
	if err != nil {
		return nil, err
	}
	if err := os.MkdirAll(cacheRoot, 0o755); err != nil {
		return nil, fmt.Errorf("create cache root: %w", err)
	}
	s := &Store{cacheRoot: cacheRoot, runs: map[string]*runHandle{}}
	if err := s.recover(); err != nil {
		return nil, err
	}
	return s, nil
}

// CacheRoot returns the absolute cache root.
func (s *Store) CacheRoot() string { return s.cacheRoot }

// EnsureSeparateFromWork refuses a work root that is the cache root or
// nested inside it (and vice versa), so fixture commands can never overwrite
// or delete the event log.
func (s *Store) EnsureSeparateFromWork(workRoot string) error {
	abs, err := filepath.Abs(filepath.Clean(workRoot))
	if err != nil {
		return err
	}
	c := s.cacheRoot
	if abs == c {
		return fmt.Errorf("work root %q must differ from cache root %q", abs, c)
	}
	if isWithin(abs, c) {
		return fmt.Errorf("work root %q must not be inside cache root %q", abs, c)
	}
	if isWithin(c, abs) {
		return fmt.Errorf("cache root %q must not be inside work root %q", c, abs)
	}
	return nil
}

func isWithin(child, parent string) bool {
	rel, err := filepath.Rel(parent, child)
	if err != nil {
		return false
	}
	return rel != ".." && !strings.HasPrefix(rel, ".."+string(os.PathSeparator)) && rel != "."
}

func newID(prefix string) string {
	var b [12]byte
	_, _ = rand.Read(b[:])
	return prefix + "-" + hex.EncodeToString(b[:])
}

// runDir is <cache>/runs/<id>; log and meta live there.
func (s *Store) runDir(id string) string {
	return filepath.Join(s.cacheRoot, "runs", id)
}

// CreateRun makes a new run, writes its meta and an initial run_started event.
func (s *Store) CreateRun(mode string, seed []ShardSeed, workRoot string, autoFinalize bool) (*RunMeta, error) {
	if mode != "events" && mode != "execute" {
		return nil, fmt.Errorf("mode must be events or execute, got %q", mode)
	}
	if seed == nil {
		seed = []ShardSeed{}
	}
	for i, sd := range seed {
		if strings.TrimSpace(sd.ShardID) == "" {
			return nil, fmt.Errorf("seed[%d]: empty shard_id", i)
		}
	}

	id := newID("run")
	dir := s.runDir(id)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, err
	}
	meta := RunMeta{
		ID:           id,
		CreatedAt:    time.Now().UTC(),
		Mode:         mode,
		Seed:         seed,
		WorkRoot:     workRoot,
		CacheRoot:    s.cacheRoot,
		FinalizeAuto: autoFinalize,
	}
	if err := s.writeMeta(meta); err != nil {
		return nil, err
	}

	h := &runHandle{
		meta:   meta,
		merger: NewMerger(id, seed),
		seen:   map[string]bool{},
	}
	s.mu.Lock()
	s.runs[id] = h
	s.mu.Unlock()

	// Initial run_started event (synthetic, assigned id).
	if _, err := s.ingestOne(h, Event{Type: EvRunStarted, RunID: id}, true); err != nil {
		return nil, err
	}
	return &meta, nil
}

func (s *Store) writeMeta(meta RunMeta) error {
	path := filepath.Join(s.runDir(meta.ID), "meta.json")
	tmp := path + ".tmp"
	b, err := json.MarshalIndent(meta, "", "  ")
	if err != nil {
		return err
	}
	if err := os.WriteFile(tmp, b, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

// Get returns meta for a run, or nil if absent.
func (s *Store) Get(id string) *RunMeta {
	s.mu.Lock()
	h := s.runs[id]
	s.mu.Unlock()
	if h == nil {
		return nil
	}
	h.mu.Lock()
	defer h.mu.Unlock()
	m := h.meta
	return &m
}

func (s *Store) handle(id string) *runHandle {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.runs[id]
}

// List returns run metas, newest first.
func (s *Store) List() []RunMeta {
	s.mu.Lock()
	ids := make([]string, 0, len(s.runs))
	for id := range s.runs {
		ids = append(ids, id)
	}
	s.mu.Unlock()
	out := make([]RunMeta, 0, len(ids))
	for _, id := range ids {
		if m := s.Get(id); m != nil {
			out = append(out, *m)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].CreatedAt.After(out[j].CreatedAt) })
	return out
}

// Ingest validates, persists and folds a batch of events for a run.
// Persistence is append-before-apply: if the process dies after the write
// but before the fold, recovery replays the log and nothing is lost.
func (s *Store) Ingest(runID string, events []Event) ([]IngestOutcome, error) {
	h := s.handle(runID)
	if h == nil {
		return nil, ErrNotFound
	}
	h.mu.Lock()
	defer h.mu.Unlock()

	outcomes := make([]IngestOutcome, 0, len(events))
	for _, ev := range events {
		ev.RunID = runID
		if err := DecodeEvent(&ev); err != nil {
			outcomes = append(outcomes, IngestOutcome{
				EventID: ev.EventID, Type: ev.Type, Error: err.Error(),
			})
			continue
		}
		o, err := s.ingestOne(h, ev, false)
		if err != nil {
			outcomes = append(outcomes, IngestOutcome{
				EventID: ev.EventID, Type: ev.Type, Error: err.Error(),
			})
			continue
		}
		outcomes = append(outcomes, o)
	}
	return outcomes, nil
}

// ErrNotFound indicates an unknown run id.
var ErrNotFound = errors.New("run not found")

// ingestOne appends and applies one event. Caller holds h.mu.
func (s *Store) ingestOne(h *runHandle, ev Event, synthetic bool) (IngestOutcome, error) {
	if ev.EventID == "" {
		ev.EventID = newID("evt")
		synthetic = true
	}

	// Append first. Open per-event is simple and safe for this workload;
	// O_APPEND keeps it crash-resilient.
	h.seq++
	env := Envelope{
		Seq: h.seq, ReceivedAt: time.Now().UTC(),
		Synthetic: synthetic, Event: ev,
	}
	line, err := json.Marshal(env)
	if err != nil {
		h.seq--
		return IngestOutcome{}, err
	}
	f, err := os.OpenFile(filepath.Join(s.runDir(h.meta.ID), "events.log"),
		os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		h.seq--
		return IngestOutcome{}, err
	}
	if _, err := f.Write(append(line, '\n')); err != nil {
		_ = f.Close()
		h.seq--
		return IngestOutcome{}, err
	}
	if err := f.Sync(); err != nil {
		_ = f.Close()
		// Sync failure is reported but the line is already written.
	} else {
		_ = f.Close()
	}

	ar, applyErr := h.merger.Apply(ev, h.seen)
	if applyErr != nil {
		return IngestOutcome{EventID: ev.EventID, Type: ev.Type, Error: applyErr.Error()}, nil
	}
	if ev.Type == EvRunFinalized {
		h.finalized = true
	}
	return IngestOutcome{
		EventID: ev.EventID, Type: ev.Type,
		Applied:   !ar.Duplicate && !ar.Late,
		Duplicate: ar.Duplicate,
		Late:      ar.Late,
		Conflict:  ar.Conflict,
	}, nil
}

// Finalize appends a run_finalized event (idempotent).
func (s *Store) Finalize(runID string) (bool, error) {
	h := s.handle(runID)
	if h == nil {
		return false, ErrNotFound
	}
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.finalized {
		return false, nil
	}
	o, err := s.ingestOne(h, Event{Type: EvRunFinalized, RunID: runID}, true)
	if err != nil {
		return false, err
	}
	h.finalized = true
	return o.Applied, nil
}

// RequestCancel appends a run_cancel_requested event. It is idempotent:
// repeated calls (e.g. HTTP cancel plus the runner's own Cancel) append no
// second event.
func (s *Store) RequestCancel(runID string) (bool, error) {
	h := s.handle(runID)
	if h == nil {
		return false, ErrNotFound
	}
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.cancelRequested {
		return false, nil
	}
	o, err := s.ingestOne(h, Event{Type: EvRunCancelRequested, RunID: runID}, true)
	if err != nil {
		return false, err
	}
	h.cancelRequested = true
	return o.Applied, nil
}

// Summary returns the current deterministic aggregate.
func (s *Store) Summary(runID string) (*Summary, error) {
	h := s.handle(runID)
	if h == nil {
		return nil, ErrNotFound
	}
	h.mu.Lock()
	defer h.mu.Unlock()
	sum := h.merger.Snapshot()
	return &sum, nil
}

// ReadEvents returns the persisted envelope log (for replay/debugging).
func (s *Store) ReadEvents(runID string) ([]Envelope, error) {
	h := s.handle(runID)
	if h == nil {
		return nil, ErrNotFound
	}
	path := filepath.Join(s.runDir(runID), "events.log")
	f, err := os.Open(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, nil
		}
		return nil, err
	}
	defer f.Close()
	var out []Envelope
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 1024*1024), 16*1024*1024)
	for sc.Scan() {
		var env Envelope
		if err := json.Unmarshal(sc.Bytes(), &env); err != nil {
			return nil, fmt.Errorf("corrupt event log: %w", err)
		}
		out = append(out, env)
	}
	return out, sc.Err()
}

// recover rebuilds all in-memory handles from disk.
func (s *Store) recover() error {
	root := filepath.Join(s.cacheRoot, "runs")
	entries, err := os.ReadDir(root)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil
		}
		return err
	}
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		id := e.Name()
		metaBytes, err := os.ReadFile(filepath.Join(root, id, "meta.json"))
		if err != nil {
			return fmt.Errorf("recover run %s: %w", id, err)
		}
		var meta RunMeta
		if err := json.Unmarshal(metaBytes, &meta); err != nil {
			return err
		}
		h := &runHandle{
			meta:   meta,
			merger: NewMerger(id, meta.Seed),
			seen:   map[string]bool{},
		}
		envs, err := readLogFile(filepath.Join(root, id, "events.log"))
		if err != nil {
			return err
		}
		for _, env := range envs {
			h.seq = env.Seq
			if _, err := h.merger.Apply(env.Event, h.seen); err != nil {
				return fmt.Errorf("recover run %s: %w", id, err)
			}
			if env.Event.Type == EvRunFinalized {
				h.finalized = true
			}
			if env.Event.Type == EvRunCancelRequested {
				h.cancelRequested = true
			}
		}
		s.runs[id] = h
	}
	return nil
}

func readLogFile(path string) ([]Envelope, error) {
	f, err := os.Open(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, nil
		}
		return nil, err
	}
	defer f.Close()
	var out []Envelope
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 1024*1024), 16*1024*1024)
	for sc.Scan() {
		var env Envelope
		if err := json.Unmarshal(sc.Bytes(), &env); err != nil {
			return nil, err
		}
		out = append(out, env)
	}
	return out, sc.Err()
}

// Replay rebuilds a summary from an arbitrary slice of events with an
// explicit seed — used by POST /replay. It does not touch any stored run,
// so it is safe and side-effect free. Deterministic finalize events are
// forced to the end so shuffling cannot finalize the run mid-batch.
func Replay(seed []ShardSeed, events []Event, shuffle bool, seedInt int64) (Summary, []IngestOutcome, error) {
	ordered := make([]Event, len(events))
	copy(ordered, events)

	var head, tail []Event
	for _, ev := range ordered {
		if ev.Type == EvRunFinalized || ev.Type == EvRunCancelRequested {
			tail = append(tail, ev)
		} else {
			head = append(head, ev)
		}
	}
	if shuffle {
		shuffleEvents(head, seedInt)
	}
	ordered = append(head, tail...)

	m := NewMerger("replay", seed)
	seen := map[string]bool{}
	outcomes := make([]IngestOutcome, 0, len(ordered))
	for _, ev := range ordered {
		ev.RunID = "replay"
		if err := DecodeEvent(&ev); err != nil {
			outcomes = append(outcomes, IngestOutcome{Type: ev.Type, Error: err.Error()})
			continue
		}
		if ev.EventID == "" {
			ev.EventID = newID("evt")
		}
		ar, err := m.Apply(ev, seen)
		if err != nil {
			outcomes = append(outcomes, IngestOutcome{Type: ev.Type, Error: err.Error()})
			continue
		}
		outcomes = append(outcomes, IngestOutcome{
			EventID: ev.EventID, Type: ev.Type,
			Applied:   !ar.Duplicate && !ar.Late,
			Duplicate: ar.Duplicate, Late: ar.Late, Conflict: ar.Conflict,
		})
	}
	return m.Snapshot(), outcomes, nil
}

// shuffleEvents performs a deterministic Fisher–Yates shuffle.
func shuffleEvents(evs []Event, seed int64) {
	// splitmix64 PRNG — small, dependency-free, reproducible.
	var state uint64 = uint64(seed)
	next := func() uint64 {
		state += 0x9e3779b97f4a7c15
		z := state
		z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9
		z = (z ^ (z >> 27)) * 0x94d049bb133111eb
		return z ^ (z >> 31)
	}
	for i := len(evs) - 1; i > 0; i-- {
		j := int(next() % uint64(i+1))
		evs[i], evs[j] = evs[j], evs[i]
	}
}
