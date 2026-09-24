package service

import (
	"context"
	"time"

	"sensorhealth/internal/domain"
	"sensorhealth/internal/store"
)

// applyHeartbeat records the heartbeat and updates last-receive timestamps.
// Heartbeats never advance sequences and never feed frozen/gap rules; they
// only prove the device is alive for the stale rule.
func (s *Service) applyHeartbeat(
	ctx context.Context,
	dev *domain.Device,
	cfg *domain.RuleConfig, cfgVer int64,
	r *store.StateRow, st *staleState,
	h IncomingMessage,
) error {
	recv := orNow(h.ReceivedAt, s.now)
	if err := s.st.InsertObservation(ctx, store.ObservationRow{
		DeviceID:   dev.ID,
		Epoch:      r.Epoch,
		SampledAt:  h.SampledAt,
		ReceivedAt: recv,
		Heartbeat:  true,
	}); err != nil {
		return err
	}
	r.LastAnyReceivedAt = recv
	// Heartbeat keeps the device alive; mark the start of a healthy streak
	// if we were not already in one.
	if !st.Open && st.ContinuousSince == nil {
		t := recv
		st.ContinuousSince = &t
	}
	if st.Open {
		// Receiving anything means the device is reachable. Recovery requires
		// continuous reachability for StaleRecoverTimeout; seed/keep the
		// streak and set the deadline on first contact.
		now := recv
		if st.HealthyDeadline == nil {
			t0 := now
			st.ContinuousSince = &t0
			dl := now.Add(cfg.StaleRecoverTimeout.Std())
			st.HealthyDeadline = &dl
		}
	}
	return nil
}

// applySample handles one sample. Returns applied=true when it was accepted as
// a NEW observation (duplicates return false, nil).
func (s *Service) applySample(
	ctx context.Context,
	dev *domain.Device,
	cfg *domain.RuleConfig, cfgVer int64,
	r *store.StateRow,
	st *staleState, fz *frozenState, gp *gapState,
	m IncomingMessage,
) (bool, error) {
	recv := orNow(m.ReceivedAt, s.now)

	// 1) Detect a new epoch (clock rollback or explicit sequence reset).
	newEpoch := s.detectNewEpoch(r, cfg, m)
	if newEpoch {
		if err := s.openNewEpoch(ctx, dev, cfg, cfgVer, r, st, fz, gp, m, recv); err != nil {
			return false, err
		}
		// First sample of the epoch.
		return s.acceptFirstSampleOfEpoch(ctx, dev, cfg, cfgVer, r, st, fz, gp, m, recv)
	}

	// 2) Within the current epoch, classify by sequence vs high watermark.
	exists, err := s.st.ExistsSample(ctx, dev.ID, r.Epoch, m.Seq)
	if err != nil {
		return false, err
	}
	if exists {
		// Duplicate replay of a known sample: idempotent no-op.
		return false, nil
	}

	if r.HighSeq == 0 || m.Seq == r.HighSeq+1 {
		// In-order continuation (or very first sample of the very first epoch).
		if r.HighSeq == 0 {
			return s.acceptFirstSampleOfEpoch(ctx, dev, cfg, cfgVer, r, st, fz, gp, m, recv)
		}
		return s.acceptForwardSample(ctx, dev, cfg, cfgVer, r, st, fz, gp, m, recv, false)
	}

	if m.Seq <= r.HighSeq {
		// Late arrival for a sequence inside the known window: batch backfill.
		// Reject if it falls outside the configured lookback window.
		if r.HighSeq-m.Seq > cfg.BackfillLookback {
			return false, ErrBackfillTooOld
		}
		return s.acceptBackfillSample(ctx, dev, cfg, cfgVer, r, st, fz, gp, m, recv)
	}

	// m.Seq > highSeq+1: forward jump with a hole.
	return s.acceptForwardSample(ctx, dev, cfg, cfgVer, r, st, fz, gp, m, recv, true)
}

// detectNewEpoch decides whether m forces a new sequence line ("epoch").
//
//  1. Sequence reset: seq is far BELOW the high watermark, beyond the
//     BackfillLookback window. Ordinary batch backfill stays close behind the
//     watermark; a sequence restarting near 1 is a new line.
//  2. Forward rebase: seq is far ABOVE the high watermark, beyond the
//     BackfillLookback window. A jump larger than any plausible backfillable
//     hole means the device began a fresh line (e.g. rebooted into a new
//     session); treating it as a gap would raise thousands of bogus missing
//     sequences.
//  3. Clock rollback: the device SampleTime jumps backwards by more than the
//     stale tolerance while the sequence keeps advancing forward. Late
//     backfill carries an older SampleTime but never has seq > highSeq, so it
//     cannot be mistaken for a rollback.
//
// Small SampleTime jitter and equal timestamps never open a new epoch.
func (s *Service) detectNewEpoch(r *store.StateRow, cfg *domain.RuleConfig, m IncomingMessage) bool {
	if r.HighSeq == 0 {
		return false
	}
	// (1) Sequence reset far below the watermark.
	if m.Seq < r.HighSeq-cfg.BackfillLookback {
		return true
	}
	// (2) Forward rebase far above the watermark.
	if m.Seq > r.HighSeq+cfg.BackfillLookback {
		return true
	}
	// (3) Forward sequence with the device clock wound backwards by more
	// than the stale tolerance.
	if m.Seq > r.HighSeq && r.LastSampleSampledAt != nil {
		if r.LastSampleSampledAt.Sub(m.SampledAt) > cfg.StaleEnterTimeout.Std() {
			return true
		}
	}
	return false
}

// openNewEpoch flushes open frozen/gap alerts (a clock rollback/reset ends the
// previous incident cleanly), bumps the epoch, and resets frozen/gap state.
// Stale state survives epoch changes because it measures server reachability.
func (s *Service) openNewEpoch(
	ctx context.Context,
	dev *domain.Device, cfg *domain.RuleConfig, cfgVer int64,
	r *store.StateRow, st *staleState, fz *frozenState, gp *gapState,
	m IncomingMessage, recv time.Time,
) error {
	// Recover any open frozen/gap alert at the epoch boundary.
	if fz.Open {
		rng := frozenRange(r, *fz)
		if _, err := s.st.RecoverAlert(ctx, dev.ID, domain.KindFrozen, rng, recv); err != nil {
			return err
		}
	}
	if gp.Open {
		rng := gapRecoverRange(r, *gp)
		if _, err := s.st.RecoverAlert(ctx, dev.ID, domain.KindGap, rng, recv); err != nil {
			return err
		}
	}
	r.Epoch++
	r.HighSeq = 0
	r.LastSampleSampledAt = nil
	r.LastSampleReceivedAt = nil
	*fz = frozenState{}
	*gp = gapState{}
	return nil
}

// acceptFirstSampleOfEpoch seeds the new epoch with seq m.Seq as the first
// sample (no gap before it).
func (s *Service) acceptFirstSampleOfEpoch(
	ctx context.Context,
	dev *domain.Device, cfg *domain.RuleConfig, cfgVer int64,
	r *store.StateRow, st *staleState, fz *frozenState, gp *gapState,
	m IncomingMessage, recv time.Time,
) (bool, error) {
	if err := s.persistSample(ctx, dev.ID, r.Epoch, m, recv); err != nil {
		return false, err
	}
	r.HighSeq = m.Seq
	gp.PeakSeq = m.Seq
	gp.FrontierSeq = m.Seq
	if !gp.HasBase {
		gp.BaseSeq = m.Seq
		gp.HasBase = true
	}
	t := m.SampledAt
	r.LastSampleSampledAt = &t
	rt := recv
	r.LastSampleReceivedAt = &rt
	r.LastAnyReceivedAt = recv
	s.markReachable(st, recv, cfg)

	if err := s.updateFrozenOnSample(ctx, dev, cfg, cfgVer, r, fz, m, recv, true); err != nil {
		return true, err
	}
	return true, nil
}

// acceptForwardSample handles seq == highSeq+1 (jumped=false) or a forward
// jump that leaves a hole (jumped=true).
func (s *Service) acceptForwardSample(
	ctx context.Context,
	dev *domain.Device, cfg *domain.RuleConfig, cfgVer int64,
	r *store.StateRow, st *staleState, fz *frozenState, gp *gapState,
	m IncomingMessage, recv time.Time, jumped bool,
) (bool, error) {
	if err := s.persistSample(ctx, dev.ID, r.Epoch, m, recv); err != nil {
		return false, err
	}

	if jumped {
		// Forward jump beyond the contiguous frontier: only PeakSeq grows;
		// FrontierSeq stays put, opening/extending the hole.
		if m.Seq > gp.PeakSeq {
			gp.PeakSeq = m.Seq
		}
	} else {
		// Contiguous next sequence: advance both frontier and peak.
		gp.FrontierSeq = m.Seq
		if m.Seq > gp.PeakSeq {
			gp.PeakSeq = m.Seq
		}
	}
	r.HighSeq = gp.PeakSeq
	t := m.SampledAt
	r.LastSampleSampledAt = &t
	rt := recv
	r.LastSampleReceivedAt = &rt
	r.LastAnyReceivedAt = recv
	s.markReachable(st, recv, cfg)

	if err := s.updateGapOnForward(ctx, dev, cfg, cfgVer, r, gp, m, recv); err != nil {
		return true, err
	}
	// A frozen value that survives a hole still continues its run (the seq
	// keeps advancing through the gap on the device side), but frozen timing
	// only counts observed samples.
	if err := s.updateFrozenOnSample(ctx, dev, cfg, cfgVer, r, fz, m, recv, false); err != nil {
		return true, err
	}
	return true, nil
}

// acceptBackfillSample fills one previously-missing sequence.
func (s *Service) acceptBackfillSample(
	ctx context.Context,
	dev *domain.Device, cfg *domain.RuleConfig, cfgVer int64,
	r *store.StateRow, st *staleState, fz *frozenState, gp *gapState,
	m IncomingMessage, recv time.Time,
) (bool, error) {
	if err := s.persistSample(ctx, dev.ID, r.Epoch, m, recv); err != nil {
		return false, err
	}
	// Backfill proves reachability too.
	r.LastAnyReceivedAt = recv
	s.markReachable(st, recv, cfg)
	// NOTE: last sample sampled-at is NOT updated: backfill is historical;
	// the device's most recent sample clock is unchanged.

	// If the new sample extends the contiguous prefix, walk the frontier
	// forward across every now-present consecutive sequence.
	if m.Seq == gp.FrontierSeq+1 {
		gp.FrontierSeq = m.Seq
		for {
			exists, err := s.st.ExistsSample(ctx, dev.ID, r.Epoch, gp.FrontierSeq+1)
			if err != nil {
				return true, err
			}
			if !exists {
				break
			}
			gp.FrontierSeq++
		}
	}

	if err := s.updateGapOnBackfill(ctx, dev, cfg, cfgVer, r, gp, m, recv); err != nil {
		return true, err
	}
	return true, nil
}

func (s *Service) persistSample(ctx context.Context, deviceID string, epoch int64, m IncomingMessage, recv time.Time) error {
	seq := m.Seq
	val := m.Value
	return s.st.InsertObservation(ctx, store.ObservationRow{
		DeviceID:   deviceID,
		Epoch:      epoch,
		Seq:        &seq,
		Value:      &val,
		SampledAt:  m.SampledAt,
		ReceivedAt: recv,
	})
}

// markReachable updates the stale-rule streak on any received message.
func (s *Service) markReachable(st *staleState, recv time.Time, cfg *domain.RuleConfig) {
	if !st.Open {
		if st.ContinuousSince == nil {
			t := recv
			st.ContinuousSince = &t
		}
		return
	}
	// Open + traffic: seed hysteresis deadline on first reachable message.
	if st.HealthyDeadline == nil {
		t0 := recv
		st.ContinuousSince = &t0
		dl := recv.Add(cfg.StaleRecoverTimeout.Std())
		st.HealthyDeadline = &dl
	}
}

func orNow(t time.Time, fallback func() time.Time) time.Time {
	if t.IsZero() {
		return fallback()
	}
	return t
}
