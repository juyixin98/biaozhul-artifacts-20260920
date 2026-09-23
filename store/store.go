// Package store provides in-memory series storage with a local write-ahead log
// (WAL). Every accepted mutation is appended as one JSON record to data/wal.log
// and fsynced before the in-memory state is updated, so a crash loses at most
// the in-flight request (and, on filesystems without a durable fsync, nothing
// more). On Open the log is replayed to reconstruct state.
//
// This is deliberately a minimal sample implementation, not a production
// database: no compaction, one global mutex, O(n) series lookup on ingest.
package store

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"sync"

	"counterreset/counter"
)

// ErrConflict means a sample with the same timestamp but a different value
// already exists for the series. Equal-value duplicates are idempotent and do
// not error.
var ErrConflict = errors.New("duplicate timestamp with different value")

// Series is one stored counter series.
type Series struct {
	Metric  string
	Labels  map[string]string
	Samples []counter.Sample
}

// walRecord is one durable mutation. Versioned for forward compatibility.
type walRecord struct {
	Version int              `json:"v"`
	Op      string           `json:"op"` // "ingest" or "reset"
	Metric  string           `json:"metric,omitempty"`
	Labels  []labelJSON      `json:"labels,omitempty"`
	Samples []counter.Sample `json:"samples,omitempty"`
}

// labelJSON is an ordered representation of labels on the wire/disk.
type labelJSON struct {
	Name  string `json:"name"`
	Value string `json:"value"`
}

// Store is the WAL-backed series store.
type Store struct {
	mu      sync.Mutex
	dir     string
	series  map[string]*Series
	file    *os.File
	enc     *json.Encoder
	unknown int // records skipped during replay (unknown op/version)
}

// Key builds the canonical series key, matching Prometheus identity: metric
// name plus labels sorted by name.
func Key(metric string, labels map[string]string) string {
	keys := make([]string, 0, len(labels))
	for k := range labels {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	b := make([]byte, 0, 64)
	b = append(b, metric...)
	for _, k := range keys {
		b = append(b, '|')
		b = append(b, k...)
		b = append(b, '=')
		b = append(b, labels[k]...)
	}
	return string(b)
}

// Open opens or creates a store backed by dir, replaying the WAL if present.
func Open(dir string) (*Store, error) {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, fmt.Errorf("create data dir: %w", err)
	}
	s := &Store{dir: dir, series: map[string]*Series{}}
	if err := s.replay(); err != nil {
		return nil, err
	}
	path := filepath.Join(dir, "wal.log")
	f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return nil, fmt.Errorf("open wal: %w", err)
	}
	s.file = f
	s.enc = json.NewEncoder(f)
	return s, nil
}

func (s *Store) walPath() string { return filepath.Join(s.dir, "wal.log") }

func (s *Store) replay() error {
	f, err := os.Open(s.walPath())
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("open wal for replay: %w", err)
	}
	defer f.Close()

	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 64*1024), 16*1024*1024)
	n := 0
	for sc.Scan() {
		n++
		line := sc.Bytes()
		if len(line) == 0 {
			continue
		}
		var r walRecord
		if err := json.Unmarshal(line, &r); err != nil {
			return fmt.Errorf("wal line %d: corrupt record: %w", n, err)
		}
		if err := s.applyRecord(r); err != nil {
			return fmt.Errorf("wal line %d: %w", n, err)
		}
	}
	return sc.Err()
}

func (s *Store) applyRecord(r walRecord) error {
	switch r.Op {
	case "ingest":
		if r.Version != 1 {
			s.unknown++
			return nil
		}
		labels := map[string]string{}
		for _, l := range r.Labels {
			labels[l.Name] = l.Value
		}
		// Replay ignores benign conflicts (same value), but a real value clash
		// on disk indicates a corrupt/incompatible log.
		if err := s.applyIngest(r.Metric, labels, r.Samples); err != nil && !errors.Is(err, ErrConflict) {
			return err
		}
	case "reset":
		s.series = map[string]*Series{}
	default:
		s.unknown++
	}
	return nil
}

// applyIngest mutates memory only. The caller (Ingest) must hold the mutex.
func (s *Store) applyIngest(metric string, labels map[string]string, samples []counter.Sample) error {
	key := Key(metric, labels)
	ser := s.series[key]
	if ser == nil {
		ser = &Series{Metric: metric, Labels: cloneLabels(labels)}
		s.series[key] = ser
	}
	byTime := map[float64]float64{}
	for _, ex := range ser.Samples {
		byTime[ex.T] = ex.Value
	}
	for _, sm := range samples {
		if v, ok := byTime[sm.T]; ok {
			if v != sm.Value {
				return fmt.Errorf("%w: t=%v stored=%v incoming=%v", ErrConflict, sm.T, v, sm.Value)
			}
			continue
		}
		ser.Samples = append(ser.Samples, sm)
		byTime[sm.T] = sm.Value
	}
	sort.SliceStable(ser.Samples, func(i, j int) bool { return ser.Samples[i].T < ser.Samples[j].T })
	return nil
}

// Ingest validates and durably appends samples to one series. Validation is
// all-or-nothing for the batch; nothing is written if any sample is bad.
// newCount counts unique timestamps new to the series; dupCount counts
// same-value duplicates (within the request or already stored) that were
// ignored idempotently.
func (s *Store) Ingest(metric string, labels map[string]string, samples []counter.Sample) (newCount, dupCount int, err error) {
	if metric == "" {
		return 0, 0, errors.New("metric name is required")
	}
	for k := range labels {
		if k == "" {
			return 0, 0, errors.New("label name must not be empty")
		}
	}
	if len(samples) == 0 {
		return 0, 0, errors.New("samples must not be empty")
	}

	// Deduplicate the request itself; validate every value.
	uniq := make([]counter.Sample, 0, len(samples))
	byReqT := map[float64]float64{}
	for _, sm := range samples {
		if e := counter.ValidateSample(sm); e != nil {
			return 0, 0, e
		}
		if v, ok := byReqT[sm.T]; ok {
			if v != sm.Value {
				return 0, 0, fmt.Errorf("%w: t=%v appears twice in request with different values", ErrConflict, sm.T)
			}
			dupCount++
			continue
		}
		byReqT[sm.T] = sm.Value
		uniq = append(uniq, sm)
	}

	s.mu.Lock()
	defer s.mu.Unlock()

	key := Key(metric, labels)
	var stored map[float64]float64
	if ser := s.series[key]; ser != nil {
		stored = map[float64]float64{}
		for _, ex := range ser.Samples {
			stored[ex.T] = ex.Value
		}
	}
	toWrite := make([]counter.Sample, 0, len(uniq))
	for _, sm := range uniq {
		if v, ok := stored[sm.T]; ok {
			if v != sm.Value {
				return 0, 0, fmt.Errorf("%w: t=%v stored=%v incoming=%v", ErrConflict, sm.T, v, sm.Value)
			}
			dupCount++
			continue
		}
		toWrite = append(toWrite, sm)
		newCount++
	}
	if len(toWrite) == 0 {
		return newCount, dupCount, nil // pure duplicate batch: nothing to log
	}

	rec := walRecord{Version: 1, Op: "ingest", Metric: metric, Samples: toWrite}
	for k, v := range labels {
		rec.Labels = append(rec.Labels, labelJSON{Name: k, Value: v})
	}
	sort.Slice(rec.Labels, func(i, j int) bool { return rec.Labels[i].Name < rec.Labels[j].Name })
	if err := s.enc.Encode(rec); err != nil {
		return 0, 0, fmt.Errorf("wal write: %w", err)
	}
	if err := s.file.Sync(); err != nil {
		return 0, 0, fmt.Errorf("wal sync: %w", err)
	}
	if err := s.applyIngest(metric, labels, toWrite); err != nil {
		return 0, 0, err
	}
	return newCount, dupCount, nil
}

// List returns all series sorted by key.
func (s *Store) List() []Series {
	s.mu.Lock()
	defer s.mu.Unlock()
	keys := make([]string, 0, len(s.series))
	for k := range s.series {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	out := make([]Series, 0, len(keys))
	for _, k := range keys {
		ser := s.series[k]
		out = append(out, Series{Metric: ser.Metric, Labels: cloneLabels(ser.Labels), Samples: cloneSamples(ser.Samples)})
	}
	return out
}

// Get returns one series and ok=false if absent.
func (s *Store) Get(metric string, labels map[string]string) (Series, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	ser, ok := s.series[Key(metric, labels)]
	if !ok {
		return Series{}, false
	}
	return Series{Metric: ser.Metric, Labels: cloneLabels(ser.Labels), Samples: cloneSamples(ser.Samples)}, true
}

// Match returns series with the given metric whose labels contain every
// selector label with the same value (extra labels allowed).
func (s *Store) Match(metric string, selector map[string]string) []Series {
	s.mu.Lock()
	defer s.mu.Unlock()
	var out []Series
	for _, ser := range s.series {
		if ser.Metric != metric {
			continue
		}
		ok := true
		for k, v := range selector {
			if ser.Labels[k] != v {
				ok = false
				break
			}
		}
		if ok {
			out = append(out, Series{Metric: ser.Metric, Labels: cloneLabels(ser.Labels), Samples: cloneSamples(ser.Samples)})
		}
	}
	sort.Slice(out, func(i, j int) bool { return Key(out[i].Metric, out[i].Labels) < Key(out[j].Metric, out[j].Labels) })
	return out
}

// Reset clears all series and persists a reset marker.
func (s *Store) Reset() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.enc.Encode(walRecord{Version: 1, Op: "reset"}); err != nil {
		return fmt.Errorf("wal write: %w", err)
	}
	if err := s.file.Sync(); err != nil {
		return fmt.Errorf("wal sync: %w", err)
	}
	s.series = map[string]*Series{}
	return nil
}

// Close flushes and closes the WAL.
func (s *Store) Close() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.file != nil {
		err := s.file.Sync()
		if e := s.file.Close(); e != nil && err == nil {
			err = e
		}
		s.file = nil
		return err
	}
	return nil
}

func cloneLabels(in map[string]string) map[string]string {
	out := make(map[string]string, len(in))
	for k, v := range in {
		out[k] = v
	}
	return out
}

func cloneSamples(in []counter.Sample) []counter.Sample {
	out := make([]counter.Sample, len(in))
	copy(out, in)
	return out
}
