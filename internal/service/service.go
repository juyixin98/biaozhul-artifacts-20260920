// Package service implements the health-determination engine.
package service

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"sync"
	"time"

	"sensorhealth/internal/domain"
	"sensorhealth/internal/store"
)

// Clock abstracts time so tests can drive the engine deterministically.
type Clock func() time.Time

// IncomingMessage is one sample or heartbeat after transport decoding.
type IncomingMessage struct {
	IsHeartbeat bool
	Seq         int64
	Value       string
	// SampledAt is the device-side sample time. For heartbeats it is the
	// heartbeat emission time.
	SampledAt time.Time
	// ReceivedAt is the server-side ingest time, assigned by the HTTP layer
	// (kept injectable for replay/backfill tests).
	ReceivedAt time.Time
}

// Service is the health engine.
type Service struct {
	st  *store.Store
	now Clock

	mu    sync.Mutex
	locks map[string]*sync.Mutex // per-device serialisation
}

func New(st *store.Store, now Clock) *Service {
	if now == nil {
		now = time.Now
	}
	return &Service{st: st, now: now, locks: make(map[string]*sync.Mutex)}
}

// deviceMu serialises all processing for one device (ingest + sweeps).
func (s *Service) deviceMu(id string) *sync.Mutex {
	s.mu.Lock()
	defer s.mu.Unlock()
	m, ok := s.locks[id]
	if !ok {
		m = &sync.Mutex{}
		s.locks[id] = m
	}
	return m
}

// ensureDevice verifies registration.
func (s *Service) ensureDevice(ctx context.Context, deviceID string) (*domain.Device, error) {
	d, err := s.st.GetDevice(ctx, deviceID)
	if errors.Is(err, store.ErrNotFound) {
		return nil, ErrUnknownDevice
	}
	return d, err
}

// config returns the current rule config + version for a device type.
func (s *Service) config(ctx context.Context, deviceType string) (domain.RuleConfig, int64, error) {
	c, ver, err := s.st.GetRuleConfig(ctx, deviceType)
	if errors.Is(err, store.ErrNotFound) {
		// Defensive: registration seeds config, but never hard-fail.
		return domain.RuleConfig{}, 0, fmt.Errorf("no rule config for device type %q", deviceType)
	}
	return c, ver, err
}

// IngestBatch accepts one signed/verified batch for a single device. Messages
// are processed deterministically: heartbeats first (sorted by sampled time),
// samples next sorted by sequence. Processing is transactional per message
// within one per-device lock; a single bad message is reported as rejected
// while the others still take effect.
func (s *Service) IngestBatch(ctx context.Context, deviceID string, msgs []IncomingMessage) (domain.IngestResult, error) {
	res := domain.IngestResult{DeviceID: deviceID}

	dev, err := s.ensureDevice(ctx, deviceID)
	if err != nil {
		return res, err
	}
	cfg, cfgVer, err := s.config(ctx, dev.Type)
	if err != nil {
		return res, err
	}

	var samples, heartbeats []IncomingMessage
	for _, m := range msgs {
		if m.IsHeartbeat {
			heartbeats = append(heartbeats, m)
		} else {
			samples = append(samples, m)
		}
	}
	sort.Slice(heartbeats, func(i, j int) bool { return heartbeats[i].SampledAt.Before(heartbeats[j].SampledAt) })
	sort.Slice(samples, func(i, j int) bool { return samples[i].Seq < samples[j].Seq })

	mu := s.deviceMu(deviceID)
	mu.Lock()
	defer mu.Unlock()

	stRow, err := s.loadOrInitState(ctx, deviceID)
	if err != nil {
		return res, err
	}
	stale, frozen, gap, err := decodeSubstates(stRow)
	if err != nil {
		return res, err
	}

	// Heartbeats: advance proof-of-life only.
	for _, h := range heartbeats {
		if err := s.applyHeartbeat(ctx, dev, &cfg, cfgVer, stRow, &stale, h); err != nil {
			res.Rejected++
			res.RejectReason = append(res.RejectReason, err.Error())
			continue
		}
		res.Accepted++
	}

	// Samples.
	var highSeq int64 = stRow.HighSeq
	for _, m := range samples {
		applied, err := s.applySample(ctx, dev, &cfg, cfgVer, stRow, &stale, &frozen, &gap, m)
		if err != nil {
			res.Rejected++
			res.RejectReason = append(res.RejectReason, err.Error())
			continue
		}
		res.Accepted++
		if applied {
			if stRow.HighSeq > highSeq {
				highSeq = stRow.HighSeq
			}
		}
	}

	// Persist state.
	if err := s.saveState(ctx, stRow, stale, frozen, gap); err != nil {
		return res, err
	}

	res.Epoch = stRow.Epoch
	res.HighSeq = stRow.HighSeq

	// Finally evaluate the stale rule at the current server time for the
	// whole batch (heartbeats/samples may have opened or begun recovery).
	if err := s.evaluateStaleLocked(ctx, dev, &cfg, cfgVer, stRow, &stale); err != nil {
		return res, err
	}
	if err := s.saveState(ctx, stRow, stale, frozen, gap); err != nil {
		return res, err
	}
	return res, nil
}

// ---------------------------------------------------------------------------
// State loading/saving
// ---------------------------------------------------------------------------

type loadedState struct {
	row    *store.StateRow
	stale  staleState
	frozen frozenState
	gap    gapState
}

func (s *Service) loadOrInitState(ctx context.Context, deviceID string) (*store.StateRow, error) {
	r, err := s.st.GetState(ctx, deviceID)
	if errors.Is(err, store.ErrNotFound) {
		r = &store.StateRow{
			DeviceID:          deviceID,
			LastAnyReceivedAt: s.now(),
			StaleJSON:         []byte(`{}`),
			FrozenJSON:        []byte(`{}`),
			GapJSON:           []byte(`{}`),
		}
		if err := s.st.PutState(ctx, *r); err != nil {
			return nil, err
		}
		return r, nil
	}
	return r, err
}

func decodeSubstates(r *store.StateRow) (staleState, frozenState, gapState, error) {
	var st staleState
	var fz frozenState
	var gp gapState
	if err := json.Unmarshal(orEmpty(r.StaleJSON), &st); err != nil {
		return st, fz, gp, err
	}
	if err := json.Unmarshal(orEmpty(r.FrozenJSON), &fz); err != nil {
		return st, fz, gp, err
	}
	if err := json.Unmarshal(orEmpty(r.GapJSON), &gp); err != nil {
		return st, fz, gp, err
	}
	return st, fz, gp, nil
}

func orEmpty(b []byte) []byte {
	if len(b) == 0 {
		return []byte(`{}`)
	}
	return b
}

func (s *Service) saveState(ctx context.Context, r *store.StateRow, st staleState, fz frozenState, gp gapState) error {
	sb, err := json.Marshal(st)
	if err != nil {
		return err
	}
	fb, err := json.Marshal(fz)
	if err != nil {
		return err
	}
	gb, err := json.Marshal(gp)
	if err != nil {
		return err
	}
	r.StaleJSON, r.FrozenJSON, r.GapJSON = sb, fb, gb
	return s.st.PutState(ctx, *r)
}
