// Package store holds the in-memory data of the service and persists it to a
// local JSON snapshot. This is a sample persistence implementation, no
// external database is required.
package store

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"sync"

	"alertfsm/internal/model"
)

const snapshotVersion = 1

// Snapshot is the on-disk representation of the whole service state.
type Snapshot struct {
	Version     int                    `json:"version"`
	ClockMS     int64                  `json:"clock_ms"`
	Rules       []model.Rule           `json:"rules"`
	States      []model.State          `json:"states"`
	Events      []model.Event          `json:"events"`
	Samples     []model.Sample         `json:"samples"`
	NextEventID int64                  `json:"next_event_id"`
	Extra       map[string]interface{} `json:"extra,omitempty"`
}

// Store is a mutex-protected in-memory database with snapshot persistence.
type Store struct {
	mu sync.Mutex

	dir string

	rules   map[string]*model.Rule
	states  map[string]*model.State
	samples map[string][]model.Sample // metric -> samples (kept sorted by ts)
	events  []model.Event

	clockMS     int64
	nextEventID int64
}

// maxSamplesPerMetric bounds memory; old samples beyond this cap are dropped.
const maxSamplesPerMetric = 10000

// New opens (or creates) a store backed by dir/snapshot.json.
func New(dir string) (*Store, error) {
	s := &Store{
		dir:         dir,
		rules:       map[string]*model.Rule{},
		states:      map[string]*model.State{},
		samples:     map[string][]model.Sample{},
		nextEventID: 1,
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, fmt.Errorf("create data dir: %w", err)
	}
	if err := s.load(); err != nil {
		return nil, err
	}
	return s, nil
}

func snapshotPath(dir string) string { return filepath.Join(dir, "snapshot.json") }

func (s *Store) load() error {
	b, err := os.ReadFile(snapshotPath(s.dir))
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("read snapshot: %w", err)
	}
	var snap Snapshot
	if err := json.Unmarshal(b, &snap); err != nil {
		return fmt.Errorf("parse snapshot: %w", err)
	}
	if snap.Version != snapshotVersion {
		return fmt.Errorf("unsupported snapshot version %d", snap.Version)
	}
	s.clockMS = snap.ClockMS
	s.nextEventID = snap.NextEventID
	for i := range snap.Rules {
		r := snap.Rules[i]
		s.rules[r.ID] = &r
	}
	for i := range snap.States {
		st := snap.States[i]
		s.states[st.RuleID] = &st
	}
	for _, sm := range snap.Samples {
		s.samples[sm.Metric] = append(s.samples[sm.Metric], sm)
	}
	s.events = snap.Events
	return nil
}

// Save writes the snapshot atomically (temp file + rename). It acquires the
// store lock itself; callers already holding the lock must use SaveLocked.
func (s *Store) Save() error {
	s.mu.Lock()
	snap := s.snapshotLocked()
	s.mu.Unlock()
	return s.writeSnapshot(snap)
}

// SaveLocked writes the snapshot while the caller already holds the lock.
func (s *Store) SaveLocked() error {
	return s.writeSnapshot(s.snapshotLocked())
}

func (s *Store) writeSnapshot(snap Snapshot) error {
	b, err := json.MarshalIndent(snap, "", "  ")
	if err != nil {
		return err
	}
	path := snapshotPath(s.dir)
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, b, 0o644); err != nil {
		return fmt.Errorf("write snapshot tmp: %w", err)
	}
	if err := os.Rename(tmp, path); err != nil {
		return fmt.Errorf("rename snapshot: %w", err)
	}
	return nil
}

func (s *Store) snapshotLocked() Snapshot {
	snap := Snapshot{
		Version:     snapshotVersion,
		ClockMS:     s.clockMS,
		NextEventID: s.nextEventID,
		Events:      append([]model.Event(nil), s.events...),
	}
	for _, r := range s.rules {
		snap.Rules = append(snap.Rules, *r)
	}
	sort.Slice(snap.Rules, func(i, j int) bool { return snap.Rules[i].ID < snap.Rules[j].ID })
	for _, st := range s.states {
		snap.States = append(snap.States, *st)
	}
	sort.Slice(snap.States, func(i, j int) bool { return snap.States[i].RuleID < snap.States[j].RuleID })
	for metric, list := range s.samples {
		snap.Samples = append(snap.Samples, list...)
		_ = metric
	}
	sort.Slice(snap.Samples, func(i, j int) bool {
		if snap.Samples[i].Metric != snap.Samples[j].Metric {
			return snap.Samples[i].Metric < snap.Samples[j].Metric
		}
		return snap.Samples[i].TSMS < snap.Samples[j].TSMS
	})
	return snap
}

// Lock/Unlock expose the store mutex so the engine can perform multi-step
// operations atomically.
func (s *Store) Lock()   { s.mu.Lock() }
func (s *Store) Unlock() { s.mu.Unlock() }

// Clock returns the virtual clock (the highest processed timestamp).
func (s *Store) Clock() int64 {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.clockMS
}

func (s *Store) SetClockLocked(t int64) {
	if t > s.clockMS {
		s.clockMS = t
	}
}

// SetClockFresh is used before any rule/sample exists to seed the clock.
func (s *Store) SetClockFresh(t int64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.clockMS = t
}

func (s *Store) ClockLocked() int64 { return s.clockMS }

// Rules

func (s *Store) ListRulesLocked() []model.Rule {
	out := make([]model.Rule, 0, len(s.rules))
	for _, r := range s.rules {
		out = append(out, *r)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ID < out[j].ID })
	return out
}

func (s *Store) GetRuleLocked(id string) (model.Rule, bool) {
	r, ok := s.rules[id]
	if !ok {
		return model.Rule{}, false
	}
	return *r, true
}

// PutRuleLocked inserts or replaces a rule. Returns the previous rule (if any).
func (s *Store) PutRuleLocked(r model.Rule) (model.Rule, bool) {
	prev, existed := s.rules[r.ID]
	cp := r
	s.rules[r.ID] = &cp
	if existed {
		return *prev, true
	}
	return model.Rule{}, false
}

func (s *Store) DeleteRuleLocked(id string) bool {
	if _, ok := s.rules[id]; !ok {
		return false
	}
	delete(s.rules, id)
	return true
}

// States

func (s *Store) GetStateLocked(ruleID string) (model.State, bool) {
	st, ok := s.states[ruleID]
	if !ok {
		return model.State{}, false
	}
	return *st, true
}

func (s *Store) PutStateLocked(st model.State) {
	cp := st
	s.states[st.RuleID] = &cp
}

func (s *Store) DeleteStateLocked(ruleID string) { delete(s.states, ruleID) }

// ListStatesLocked returns states ordered by rule id.
func (s *Store) ListStatesLocked() []model.State {
	out := make([]model.State, 0, len(s.states))
	for _, st := range s.states {
		out = append(out, *st)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].RuleID < out[j].RuleID })
	return out
}

// Events

// AddEventLocked assigns a sequential id and stores the event.
func (s *Store) AddEventLocked(e model.Event) model.Event {
	e.ID = s.nextEventID
	s.nextEventID++
	s.events = append(s.events, e)
	return e
}

// ListEvents returns up to limit events newer than afterID (0 = from start),
// optionally filtered by rule.
func (s *Store) ListEvents(afterID int64, ruleID string, limit int) []model.Event {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]model.Event, 0)
	for _, e := range s.events {
		if e.ID <= afterID {
			continue
		}
		if ruleID != "" && e.RuleID != ruleID {
			continue
		}
		out = append(out, e)
	}
	if limit > 0 && len(out) > limit {
		out = out[len(out)-limit:]
	}
	return out
}

// Samples

// AddSampleLocked stores a sample. It is an upsert keyed by (metric, ts):
// returns true if a sample with the same metric+timestamp already existed
// (duplicate). Samples are kept sorted by timestamp.
func (s *Store) AddSampleLocked(sm model.Sample) bool {
	list := s.samples[sm.Metric]
	// Fast path: after the last element.
	if len(list) > 0 && sm.TSMS >= list[len(list)-1].TSMS {
		if sm.TSMS == list[len(list)-1].TSMS {
			return true // duplicate: first write wins (idempotent ingestion)
		}
		s.samples[sm.Metric] = append(list, sm)
		return false
	}
	i := sort.Search(len(list), func(i int) bool { return list[i].TSMS >= sm.TSMS })
	if i < len(list) && list[i].TSMS == sm.TSMS {
		return true // duplicate: first write wins
	}
	list = append(list, model.Sample{})
	copy(list[i+1:], list[i:])
	list[i] = sm
	if len(list) > maxSamplesPerMetric {
		list = list[len(list)-maxSamplesPerMetric:]
	}
	s.samples[sm.Metric] = list
	return false
}

// HasSampleLocked reports whether a (metric, ts) sample is stored.
func (s *Store) HasSampleLocked(metric string, ts int64) bool {
	list := s.samples[metric]
	i := sort.Search(len(list), func(i int) bool { return list[i].TSMS >= ts })
	return i < len(list) && list[i].TSMS == ts
}

// ListSamples returns samples for a metric within [fromMS, toMS] (0 = unbounded).
func (s *Store) ListSamples(metric string, fromMS, toMS int64) []model.Sample {
	s.mu.Lock()
	defer s.mu.Unlock()
	list := s.samples[metric]
	out := make([]model.Sample, 0)
	for _, sm := range list {
		if fromMS > 0 && sm.TSMS < fromMS {
			continue
		}
		if toMS > 0 && sm.TSMS > toMS {
			continue
		}
		out = append(out, sm)
	}
	return out
}
