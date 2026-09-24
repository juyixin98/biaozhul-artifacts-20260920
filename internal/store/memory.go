package store

import (
	"context"
	"sync"
	"time"

	"github.com/example/rollout/internal/models"
)

// Memory is an in-memory Store used by unit tests. It models the same
// transactional and optimistic-locking semantics as the PostgreSQL store.
type Memory struct {
	mu sync.Mutex

	thresholds   []models.ThresholdVersion
	releases     map[string]models.Release
	events       []models.Event
	observations []Observation
	nonces       map[string]int64 // digest -> expiry unix
	idem         map[string]StoredResponse

	eventSeq int64
	obsSeq   int64
}

func NewMemory() *Memory {
	m := &Memory{
		releases: map[string]models.Release{},
		nonces:   map[string]int64{},
		idem:     map[string]StoredResponse{},
	}
	m.thresholds = append(m.thresholds, models.ThresholdVersion{
		Version:     1,
		Spec:        models.DefaultThresholdSpec(),
		CreatedAt:   time.Now().UTC(),
		Description: "initial default policy",
	})
	return m
}

func (m *Memory) Migrate(_ context.Context) error { return nil }
func (m *Memory) Ping(_ context.Context) error    { return nil }

func (m *Memory) GetLatestThreshold(_ context.Context) (models.ThresholdVersion, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.thresholds[len(m.thresholds)-1], nil
}

func (m *Memory) GetThreshold(_ context.Context, version int) (models.ThresholdVersion, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	for _, t := range m.thresholds {
		if t.Version == version {
			return t, nil
		}
	}
	return models.ThresholdVersion{}, ErrNotFound
}

func (m *Memory) CreateThreshold(_ context.Context, spec models.ThresholdSpec, description string) (models.ThresholdVersion, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	t := models.ThresholdVersion{
		Version:     m.thresholds[len(m.thresholds)-1].Version + 1,
		Spec:        spec,
		CreatedAt:   time.Now().UTC(),
		Description: description,
	}
	m.thresholds = append(m.thresholds, t)
	return t, nil
}

func (m *Memory) CreateRelease(_ context.Context, r models.Release) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if _, ok := m.releases[r.ID]; ok {
		return ErrConflict
	}
	m.releases[r.ID] = r
	return nil
}

func (m *Memory) GetRelease(_ context.Context, id string) (models.Release, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	r, ok := m.releases[id]
	if !ok {
		return models.Release{}, ErrNotFound
	}
	return r, nil
}

func (m *Memory) ListReleases(_ context.Context) ([]models.Release, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := make([]models.Release, 0, len(m.releases))
	for _, r := range m.releases {
		out = append(out, r)
	}
	return out, nil
}

func (m *Memory) ListEvents(_ context.Context, releaseID string) ([]models.Event, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := []models.Event{}
	for _, e := range m.events {
		if e.ReleaseID == releaseID {
			out = append(out, e)
		}
	}
	return out, nil
}

func (m *Memory) ListObservations(_ context.Context, releaseID string) ([]Observation, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := []Observation{}
	for _, o := range m.observations {
		if o.ReleaseID == releaseID {
			out = append(out, o)
		}
	}
	return out, nil
}

func (m *Memory) InsertObservationDirect(_ context.Context, o Observation) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.obsSeq++
	o.ID = m.obsSeq
	if o.CreatedAt == "" {
		o.CreatedAt = time.Now().UTC().Format(time.RFC3339Nano)
	}
	m.observations = append(m.observations, o)
	return nil
}

func (m *Memory) ConsumeNonce(_ context.Context, digest string, expiresAtUnix int64) (bool, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if exp, ok := m.nonces[digest]; ok && exp > time.Now().Unix() {
		return false, nil
	}
	m.nonces[digest] = expiresAtUnix
	return true, nil
}

func (m *Memory) GetIdempotentResponse(_ context.Context, digest string) (StoredResponse, bool, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	r, ok := m.idem[digest]
	return r, ok, nil
}

func (m *Memory) PutIdempotentResponse(_ context.Context, digest string, resp StoredResponse) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.idem[digest] = resp
	return nil
}

type memTx struct {
	m      *Memory
	locked map[string]models.Release
}

func (m *Memory) WithTx(_ context.Context, fn func(tx Tx) error) error {
	m.mu.Lock()
	t := &memTx{m: m, locked: map[string]models.Release{}}
	err := fn(t)
	if err != nil {
		m.mu.Unlock()
		return err
	}
	for id, r := range t.locked {
		m.releases[id] = r
	}
	m.mu.Unlock()
	return nil
}

func (t *memTx) GetReleaseForUpdate(_ context.Context, id string) (models.Release, error) {
	if r, ok := t.locked[id]; ok {
		return r, nil
	}
	r, ok := t.m.releases[id]
	if !ok {
		return models.Release{}, ErrNotFound
	}
	t.locked[id] = r
	return r, nil
}

func (t *memTx) UpdateRelease(_ context.Context, r models.Release) error {
	if _, ok := t.m.releases[r.ID]; !ok {
		return ErrNotFound
	}
	t.locked[r.ID] = r
	return nil
}

func (t *memTx) InsertEvent(_ context.Context, e models.Event) error {
	t.m.eventSeq++
	e.Seq = t.m.eventSeq
	if e.CreatedAt.IsZero() {
		e.CreatedAt = time.Now().UTC()
	}
	t.m.events = append(t.m.events, e)
	return nil
}

func (t *memTx) InsertObservation(_ context.Context, o Observation) error {
	t.m.obsSeq++
	o.ID = t.m.obsSeq
	if o.CreatedAt == "" {
		o.CreatedAt = time.Now().UTC().Format(time.RFC3339Nano)
	}
	t.m.observations = append(t.m.observations, o)
	return nil
}
