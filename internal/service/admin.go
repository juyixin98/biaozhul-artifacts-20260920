package service

import (
	"context"
	"errors"
	"time"

	"sensorhealth/internal/config"
	"sensorhealth/internal/domain"
	"sensorhealth/internal/store"
)

var (
	ErrUnknownDevice  = errors.New("unknown device")
	ErrBackfillTooOld = errors.New("backfilled sequence is older than the configured lookback window")
)

// RegisterDevice registers (or retypes) a device and seeds its rule config for
// the type if none exists.
func (s *Service) RegisterDevice(ctx context.Context, id, devType string) (*domain.Device, int64, error) {
	if id == "" || devType == "" {
		return nil, 0, errors.New("id and type are required")
	}
	// Seed config for the type if missing.
	if _, _, err := s.st.GetRuleConfig(ctx, devType); errors.Is(err, store.ErrNotFound) {
		c := config.DefaultRuleConfig(devType, 0)
		if _, err := s.st.PutRuleConfig(ctx, c); err != nil {
			return nil, 0, err
		}
	} else if err != nil {
		return nil, 0, err
	}
	d := domain.Device{ID: id, Type: devType, RegisteredAt: s.now()}
	if err := s.st.UpsertDevice(ctx, d); err != nil {
		return nil, 0, err
	}
	// Ensure state row exists.
	if _, err := s.loadOrInitState(ctx, id); err != nil {
		return nil, 0, err
	}
	cfg, ver, err := s.st.GetRuleConfig(ctx, devType)
	if err != nil {
		return nil, 0, err
	}
	_ = cfg
	return &d, ver, nil
}

// GetConfig returns the effective config for a device type.
func (s *Service) GetConfig(ctx context.Context, devType string) (domain.RuleConfig, error) {
	c, _, err := s.st.GetRuleConfig(ctx, devType)
	return c, err
}

// UpdateConfig validates and stores a new config version.
func (s *Service) UpdateConfig(ctx context.Context, c domain.RuleConfig) (int64, error) {
	if err := config.Validate(c); err != nil {
		return 0, err
	}
	return s.st.PutRuleConfig(ctx, c)
}

// ListAlerts exposes the alert history.
func (s *Service) ListAlerts(ctx context.Context, deviceID string, status domain.AlertStatus, kind domain.Kind, limit int) ([]domain.Alert, error) {
	return s.st.ListAlerts(ctx, deviceID, status, kind, limit)
}

// Health builds the current snapshot for one device.
func (s *Service) Health(ctx context.Context, deviceID string) (*domain.Health, error) {
	dev, err := s.ensureDevice(ctx, deviceID)
	if err != nil {
		return nil, err
	}
	cfg, cfgVer, err := s.config(ctx, dev.Type)
	if err != nil {
		return nil, err
	}
	r, err := s.loadOrInitState(ctx, deviceID)
	if err != nil {
		return nil, err
	}
	st, fz, gp, err := decodeSubstates(r)
	if err != nil {
		return nil, err
	}

	h := &domain.Health{
		DeviceID:      dev.ID,
		DeviceType:    dev.Type,
		ConfigVersion: cfgVer,
		Epoch:         r.Epoch,
		LastSeq:       r.HighSeq,
		LastReceiveAt: r.LastAnyReceivedAt,
		Details:       map[domain.Kind]domain.AlertDetail{},
	}
	if r.LastSampleSampledAt != nil {
		t := *r.LastSampleSampledAt
		h.LastSampleAt = &t
	}

	attach := func(k domain.Kind, open bool, rng domain.SampleRange, since time.Time, extra map[string]any) {
		status := domain.StatusRecovered
		if open {
			status = domain.StatusOpen
			h.OpenAlerts = append(h.OpenAlerts, k)
		}
		h.Details[k] = domain.AlertDetail{Status: status, Range: rng, Since: since, Extra: extra}
	}

	now := s.now()
	silence := now.Sub(r.LastAnyReceivedAt)
	if st.Open {
		since := orNowPtr(st.Since, r.LastAnyReceivedAt)
		attach(domain.KindStale, true,
			domain.SampleRange{Epoch: r.Epoch, SeqStart: r.HighSeq, SeqEnd: r.HighSeq,
				FirstTime: r.LastAnyReceivedAt, LastTime: r.LastAnyReceivedAt},
			since, map[string]any{"silence": silence.String()})
	} else {
		attach(domain.KindStale, false,
			domain.SampleRange{Epoch: r.Epoch, SeqStart: r.HighSeq, SeqEnd: r.HighSeq,
				FirstTime: r.LastAnyReceivedAt, LastTime: now},
			r.LastAnyReceivedAt, map[string]any{"silence": silence.String()})
	}

	if fz.RunCount > 0 || fz.Open {
		rng := domain.SampleRange{
			Epoch: r.Epoch, SeqStart: fz.RunStartSeq,
			SeqEnd:    fz.RunLastSeq,
			FirstTime: ptrTimeOr(fz.RunStartAt, r.LastAnyReceivedAt),
			LastTime:  ptrTimeOr(fz.RunLastAt, r.LastAnyReceivedAt),
		}
		extra := map[string]any{
			"run_count":   fz.RunCount,
			"value":       fz.Value,
			"enter_count": cfg.FrozenEnterCount,
		}
		attach(domain.KindFrozen, fz.Open, rng, ptrTimeOr(fz.RunStartAt, r.LastAnyReceivedAt), extra)
	}

	if gp.PeakSeq > 0 {
		rng := domain.SampleRange{
			Epoch:    r.Epoch,
			SeqStart: gp.FrontierSeq + 1, SeqEnd: gp.PeakSeq,
			FirstTime: r.LastAnyReceivedAt, LastTime: now}
		attach(domain.KindGap, gp.Open, rng, r.LastAnyReceivedAt, map[string]any{
			"frontier_seq": gp.FrontierSeq,
			"peak_seq":     gp.PeakSeq,
			"missing":      gp.PeakSeq - gp.FrontierSeq,
		})
	}

	h.Healthy = len(h.OpenAlerts) == 0
	return h, nil
}

// StartStaleSweeper runs background stale evaluation on an interval until ctx
// is cancelled.
func (s *Service) StartStaleSweeper(ctx context.Context, interval time.Duration) {
	go func() {
		t := time.NewTicker(interval)
		defer t.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-t.C:
				_ = s.SweepStale(ctx, s.now())
			}
		}
	}()
}

func orNowPtr(p *time.Time, fallback time.Time) time.Time {
	if p != nil {
		return *p
	}
	return fallback
}
