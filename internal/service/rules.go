package service

import (
	"context"
	"fmt"
	"time"

	"sensorhealth/internal/domain"
	"sensorhealth/internal/store"
)

// ===========================================================================
// FROZEN — constant value with advancing sequence numbers.
// ===========================================================================

// updateFrozenOnSample advances the equal-value run machine.
//
// Entry requires BOTH:
//   - FrozenEnterCount consecutive samples carrying the SAME value, AND
//   - those samples spanning at least FrozenEnterMinDuration of *device*
//     SampleTime.
//
// The sequence MUST advance across every sample (we only process one
// observation per seq and duplicates are ignored), so a legitimately
// stationary device whose samples advance but report the same reading for a
// short window is not flagged: a type whose readings are physically stable
// (e.g. a door contact) simply gets a larger FrozenEnterCount / longer
// FrozenEnterMinDuration in its own config.
func (s *Service) updateFrozenOnSample(
	ctx context.Context,
	dev *domain.Device, cfg *domain.RuleConfig, cfgVer int64,
	r *store.StateRow, fz *frozenState,
	m IncomingMessage, recv time.Time, firstOfEpoch bool,
) error {
	if !fz.Open {
		if fz.RunCount == 0 || fz.Value != m.Value {
			// Start/restart the equal-value run.
			fz.Value = m.Value
			fz.RunStartSeq = m.Seq
			fz.RunLastSeq = m.Seq
			t0 := m.SampledAt
			fz.RunStartAt = &t0
			t1 := m.SampledAt
			fz.RunLastAt = &t1
			fz.RunCount = 1
			return nil
		}
		// Same value, advancing seq. Only forward samples extend the run;
		// backfilled samples are historical and were absent when the run was
		// evaluated, so they neither extend nor break it.
		if m.Seq > fz.RunLastSeq {
			fz.RunCount++
			fz.RunLastSeq = m.Seq
			t1 := m.SampledAt
			fz.RunLastAt = &t1
		}

		span := time.Duration(0)
		if fz.RunStartAt != nil {
			span = fz.RunLastAt.Sub(*fz.RunStartAt)
		}
		if fz.RunCount >= cfg.FrozenEnterCount && span >= cfg.FrozenEnterMinDuration.Std() {
			return s.openFrozen(ctx, dev, cfg, cfgVer, r, fz, recv)
		}
		return nil
	}

	// Alert already open: count DIFFERENT values for hysteresis recovery.
	if m.Value == fz.Value {
		// Same frozen value again: recovery run resets.
		fz.RecoverCount = 0
		fz.RecStartSeq, fz.RecEndSeq = 0, 0
		fz.RecStartAt, fz.RecEndAt = nil, nil
		return nil
	}
	if fz.RecoverCount == 0 {
		fz.RecStartSeq = m.Seq
		t0 := m.SampledAt
		fz.RecStartAt = &t0
	}
	fz.RecoverCount++
	fz.RecEndSeq = m.Seq
	t1 := m.SampledAt
	fz.RecEndAt = &t1

	if fz.RecoverCount >= cfg.FrozenRecoverCount {
		rng := domain.SampleRange{
			Epoch:     r.Epoch,
			SeqStart:  fz.RecStartSeq,
			SeqEnd:    fz.RecEndSeq,
			FirstTime: ptrTimeOr(fz.RecStartAt, m.SampledAt),
			LastTime:  ptrTimeOr(fz.RecEndAt, m.SampledAt),
		}
		if _, err := s.st.RecoverAlert(ctx, dev.ID, domain.KindFrozen, rng, recv); err != nil {
			return err
		}
		// Reset run machine for a fresh start.
		*fz = frozenState{
			Open: false, Value: m.Value,
			RunStartSeq: m.Seq, RunLastSeq: m.Seq, RunCount: 1,
			RunStartAt: &t1, RunLastAt: &t1,
		}
	}
	return nil
}

func (s *Service) openFrozen(
	ctx context.Context,
	dev *domain.Device, cfg *domain.RuleConfig, cfgVer int64,
	r *store.StateRow, fz *frozenState, recv time.Time,
) error {
	rng := domain.SampleRange{
		Epoch:     r.Epoch,
		SeqStart:  fz.RunStartSeq,
		SeqEnd:    fz.RunLastSeq,
		FirstTime: ptrTimeOr(fz.RunStartAt, recv),
		LastTime:  ptrTimeOr(fz.RunLastAt, recv),
	}

	a := &domain.Alert{
		DeviceID:      dev.ID,
		DeviceType:    dev.Type,
		Kind:          domain.KindFrozen,
		Status:        domain.StatusOpen,
		ConfigVersion: cfgVer,
		TriggerRange:  rng,
		OpenedAt:      recv,
		Detail: fmt.Sprintf("value %q constant across %d samples spanning %s of device time (>= %d / %s threshold)",
			fz.Value, fz.RunCount,
			fz.RunLastAt.Sub(*fz.RunStartAt).Truncate(time.Second),
			cfg.FrozenEnterCount, cfg.FrozenEnterMinDuration.Std()),
	}
	if err := s.st.InsertAlert(ctx, a); err != nil {
		return err
	}
	fz.Open = true
	fz.RecoverCount = 0
	return nil
}

func frozenRange(r *store.StateRow, fz frozenState) domain.SampleRange {
	return domain.SampleRange{
		Epoch:     r.Epoch,
		SeqStart:  fz.RunStartSeq,
		SeqEnd:    r.HighSeq,
		FirstTime: ptrTimeOr(fz.RunStartAt, r.LastAnyReceivedAt),
		LastTime:  ptrTimeOr(fz.RunLastAt, r.LastAnyReceivedAt),
	}
}

// ===========================================================================
// GAP — sequence holes, recoverable by out-of-order batch backfill.
// ===========================================================================

// holeMissing returns the number of absent sequences in the currently open
// hole (FrontierSeq, PeakSeq].
func holeMissing(gp *gapState) int64 {
	if gp.PeakSeq <= gp.FrontierSeq {
		return 0
	}
	return gp.PeakSeq - gp.FrontierSeq
}

// updateGapOnForward reacts to a forward sample. When the jump leaves at least
// MissingEnterCount missing sequences at the contiguous frontier, the alert
// opens. The trigger interval is exactly [frontier+1, peak].
func (s *Service) updateGapOnForward(
	ctx context.Context,
	dev *domain.Device, cfg *domain.RuleConfig, cfgVer int64,
	r *store.StateRow, gp *gapState,
	m IncomingMessage, recv time.Time,
) error {
	if gp.Open {
		return nil
	}
	missing := holeMissing(gp)
	if missing < int64(cfg.MissingEnterCount) {
		return nil
	}

	first := gp.FrontierSeq + 1
	last := gp.PeakSeq
	a := &domain.Alert{
		DeviceID:      dev.ID,
		DeviceType:    dev.Type,
		Kind:          domain.KindGap,
		Status:        domain.StatusOpen,
		ConfigVersion: cfgVer,
		TriggerRange: domain.SampleRange{
			Epoch: r.Epoch, SeqStart: first, SeqEnd: last,
			FirstTime: m.SampledAt, // device time of the jump target
			LastTime:  m.SampledAt,
		},
		OpenedAt: recv,
		Detail: fmt.Sprintf("%d missing sequence(s) after contiguous seq %d, jump to %d (threshold %d)",
			missing, gp.FrontierSeq, last, cfg.MissingEnterCount),
	}
	if err := s.st.InsertAlert(ctx, a); err != nil {
		return err
	}
	gp.Open = true
	gp.TrigStartSeq, gp.TrigEndSeq = first, last
	t0 := m.SampledAt
	gp.TrigFirstAt, gp.TrigLastAt = &t0, &t0
	gp.RecStartSeq, gp.RecEndSeq = 0, 0
	gp.RecFirstAt, gp.RecEndAt = nil, nil
	return nil
}

// updateGapOnBackfill reacts to a late sample (and the frontier advance the
// caller performed); it recovers the alert once the open hole shrinks below
// MissingRecoverCount.
func (s *Service) updateGapOnBackfill(
	ctx context.Context,
	dev *domain.Device, cfg *domain.RuleConfig, cfgVer int64,
	r *store.StateRow, gp *gapState,
	m IncomingMessage, recv time.Time,
) error {
	// Record the span of samples that are healing the hole.
	if gp.RecStartSeq == 0 || m.Seq < gp.RecStartSeq {
		gp.RecStartSeq = m.Seq
		t0 := m.SampledAt
		gp.RecFirstAt = &t0
	}
	if m.Seq > gp.RecEndSeq {
		gp.RecEndSeq = m.Seq
		t1 := m.SampledAt
		gp.RecEndAt = &t1
	}
	// The contiguous frontier is the true heal point.
	if gp.FrontierSeq > gp.RecEndSeq {
		gp.RecEndSeq = gp.FrontierSeq
		t := ptrTimeOr(r.LastSampleReceivedAt, recv)
		gp.RecEndAt = &t
	}
	if gp.FrontierSeq < gp.RecStartSeq {
		gp.RecStartSeq = gp.FrontierSeq
	}

	if !gp.Open {
		return nil
	}
	if holeMissing(gp) >= int64(cfg.MissingRecoverCount) {
		return nil
	}

	rng := domain.SampleRange{
		Epoch:    r.Epoch,
		SeqStart: gp.RecStartSeq, SeqEnd: gp.RecEndSeq,
		FirstTime: ptrTimeOr(gp.RecFirstAt, m.SampledAt),
		LastTime:  ptrTimeOr(gp.RecEndAt, m.SampledAt),
	}
	if _, err := s.st.RecoverAlert(ctx, dev.ID, domain.KindGap, rng, recv); err != nil {
		return err
	}
	gp.Open = false
	gp.TrigStartSeq, gp.TrigEndSeq = 0, 0
	gp.TrigFirstAt, gp.TrigLastAt = nil, nil
	gp.RecStartSeq, gp.RecEndSeq = 0, 0
	gp.RecFirstAt, gp.RecEndAt = nil, nil
	return nil
}

func gapRecoverRange(r *store.StateRow, gp gapState) domain.SampleRange {
	return domain.SampleRange{
		Epoch:     r.Epoch,
		SeqStart:  orZero64(gp.RecStartSeq, r.HighSeq),
		SeqEnd:    orZero64(gp.RecEndSeq, r.HighSeq),
		FirstTime: ptrTimeOr(gp.RecFirstAt, r.LastAnyReceivedAt),
		LastTime:  ptrTimeOr(gp.RecEndAt, r.LastAnyReceivedAt),
	}
}

// ===========================================================================
// STALE — reachability on the SERVER clock (heartbeats keep it alive).
// ===========================================================================

// SweepStale is invoked both by the background ticker and synchronously at the
// end of ingest. It transitions stale alerts using distinct entry and recovery
// timeouts.
func (s *Service) SweepStale(ctx context.Context, now time.Time) error {
	ids, err := s.st.ListStateDeviceIDs(ctx)
	if err != nil {
		return err
	}
	for _, id := range ids {
		if err := s.sweepOneStale(ctx, id, now); err != nil {
			return err
		}
	}
	return nil
}

func (s *Service) sweepOneStale(ctx context.Context, deviceID string, now time.Time) error {
	mu := s.deviceMu(deviceID)
	mu.Lock()
	defer mu.Unlock()

	dev, err := s.st.GetDevice(ctx, deviceID)
	if err != nil {
		return err
	}
	cfg, cfgVer, err := s.config(ctx, dev.Type)
	if err != nil {
		return err
	}
	r, err := s.loadOrInitState(ctx, deviceID)
	if err != nil {
		return err
	}
	st, fz, gp, err := decodeSubstates(r)
	if err != nil {
		return err
	}
	if err := s.transitionStale(ctx, dev, &cfg, cfgVer, r, &st, now); err != nil {
		return err
	}
	return s.saveState(ctx, r, st, fz, gp)
}

// evaluateStaleLocked is the post-ingest sweep variant (caller holds the
// per-device lock).
func (s *Service) evaluateStaleLocked(
	ctx context.Context,
	dev *domain.Device, cfg *domain.RuleConfig, cfgVer int64,
	r *store.StateRow, st *staleState,
) error {
	return s.transitionStale(ctx, dev, cfg, cfgVer, r, st, s.now())
}

func (s *Service) transitionStale(
	ctx context.Context,
	dev *domain.Device, cfg *domain.RuleConfig, cfgVer int64,
	r *store.StateRow, st *staleState, now time.Time,
) error {
	silence := now.Sub(r.LastAnyReceivedAt)

	if !st.Open {
		if silence >= cfg.StaleEnterTimeout.Std() {
			// Open at the moment the threshold was crossed.
			openedAt := r.LastAnyReceivedAt.Add(cfg.StaleEnterTimeout.Std())
			var seqStart, seqEnd int64
			var firstT, lastT time.Time
			if r.LastSampleSampledAt != nil {
				seqStart = r.HighSeq
				seqEnd = r.HighSeq
				firstT = *r.LastSampleSampledAt
				lastT = *r.LastSampleSampledAt
			} else {
				firstT = r.LastAnyReceivedAt
				lastT = r.LastAnyReceivedAt
			}
			a := &domain.Alert{
				DeviceID:      dev.ID,
				DeviceType:    dev.Type,
				Kind:          domain.KindStale,
				Status:        domain.StatusOpen,
				ConfigVersion: cfgVer,
				TriggerRange: domain.SampleRange{
					Epoch: r.Epoch, SeqStart: seqStart, SeqEnd: seqEnd,
					FirstTime: firstT, LastTime: lastT,
				},
				OpenedAt: openedAt,
				Detail: fmt.Sprintf("no message (sample or heartbeat) for %s (threshold %s)",
					silence.Truncate(time.Millisecond), cfg.StaleEnterTimeout.Std()),
			}
			if err := s.st.InsertAlert(ctx, a); err != nil {
				return err
			}
			st.Open = true
			t := openedAt
			st.Since = &t
			st.HealthyDeadline = nil
			st.ContinuousSince = nil
		}
		return nil
	}

	// Currently open.
	if silence >= cfg.StaleEnterTimeout.Std() {
		// Still silent: reset any recovery streak/deadline.
		st.HealthyDeadline = nil
		st.ContinuousSince = nil
		return nil
	}

	// Some traffic exists (silence < enter timeout). Recovery requires
	// CONTINUOUS reachability for the shorter recover timeout.
	if st.HealthyDeadline == nil {
		// Should be seeded on ingest, but defend for the restart case: if the
		// persisted state shows traffic, seed now.
		t0 := r.LastAnyReceivedAt
		st.ContinuousSince = &t0
		dl := t0.Add(cfg.StaleRecoverTimeout.Std())
		st.HealthyDeadline = &dl
	}
	if !now.Before(*st.HealthyDeadline) {
		// Continuous streak intact? It is intact iff no message gap since
		// ContinuousSince exceeded the enter timeout; LastAnyReceivedAt being
		// within the enter timeout and the deadline having elapsed suffices
		// because any silence would have reset the deadline via this branch.
		rng := domain.SampleRange{
			Epoch:    r.Epoch,
			SeqStart: orZero64(r.HighSeq, 0), SeqEnd: r.HighSeq,
			FirstTime: r.LastAnyReceivedAt, LastTime: now,
		}
		if _, err := s.st.RecoverAlert(ctx, dev.ID, domain.KindStale, rng, now); err != nil {
			return err
		}
		*st = staleState{}
		t := now
		st.ContinuousSince = &t
	}
	return nil
}

func ptrTimeOr(p *time.Time, fallback time.Time) time.Time {
	if p != nil {
		return *p
	}
	return fallback
}

func orZero64(v, fallback int64) int64 {
	if v == 0 {
		return fallback
	}
	return v
}
