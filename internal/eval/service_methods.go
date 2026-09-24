package eval

import (
	"context"
	"errors"
	"fmt"

	"github.com/example/rollout/internal/crypto"
	"github.com/example/rollout/internal/models"
	"github.com/example/rollout/internal/store"
)

// Validation errors map to 400; command rejections map to 409.
var (
	ErrBadInput       = errors.New("bad input")
	ErrNotAllowed     = errors.New("command not allowed by current verdict")
	ErrPaused         = errors.New("release is paused")
	ErrTerminal       = errors.New("release is in a terminal state")
	ErrUnknownVerdict = errors.New("verdict unknown: full observation window or minimum samples not satisfied")
)

// Rejection describes why a command was refused; it is both returned to the
// caller and appended to the audit log.
type Rejection struct {
	Command models.Command `json:"command"`
	Reason  string         `json:"reason"`
	Verdict models.Verdict `json:"verdict"`
}

// CreateRelease starts a rollout at 5%, freezing the current threshold policy.
func (s *Releaser) CreateRelease(ctx context.Context, in CreateReleaseInput, defaultObservationMS, defaultMinSamples int64, defaultMetricURL string) (models.Release, error) {
	if in.Name == "" || in.Version == "" {
		return models.Release{}, fmt.Errorf("%w: name and version are required", ErrBadInput)
	}
	if in.Scenario == "" {
		in.Scenario = "healthy"
	}
	obs, mins := in.ObservationMS, in.MinSamples
	if obs <= 0 {
		obs = defaultObservationMS
	}
	if mins <= 0 {
		mins = defaultMinSamples
	}
	url := in.MetricURL
	if url == "" {
		url = defaultMetricURL
	}

	tv, err := s.Store.GetLatestThreshold(ctx)
	if err != nil {
		return models.Release{}, err
	}
	id, err := crypto.NewID("rel")
	if err != nil {
		return models.Release{}, err
	}
	now := s.Now().UTC()
	now = alignWindow(now)
	r := models.Release{
		ID:               id,
		Name:             in.Name,
		Version:          in.Version,
		State:            models.StateActive,
		Stage:            models.Stage5,
		StageWeight:      weightOf(models.Stage5),
		Generation:       0,
		ObservationMS:    obs,
		MinSamples:       mins,
		ThresholdVersion: tv.Version,
		ThresholdSpec:    tv.Spec,
		MetricURL:        url,
		Scenario:         in.Scenario,
		StageEnteredAt:   now,
		CreatedAt:        now,
		UpdatedAt:        now,
	}
	if err := s.Store.CreateRelease(ctx, r); err != nil {
		return models.Release{}, err
	}
	_ = s.Store.WithTx(ctx, func(tx store.Tx) error {
		return tx.InsertEvent(ctx, models.Event{
			ReleaseID:  r.ID,
			Generation: 0,
			Type:       "release_created",
			ToStage:    models.Stage5,
			Detail:     fmt.Sprintf("threshold policy frozen at v%d (err<=%.4f, latency_mean<%.0fms)", tv.Version, tv.Spec.ErrorRateUpper, tv.Spec.LatencyMeanMS),
			CreatedAt:  now,
		})
	})
	return r, nil
}

// CommandResult is returned after an applied or refused command.
type CommandResult struct {
	Applied bool            `json:"applied"`
	Release models.Release  `json:"release"`
	Verdict *models.Verdict `json:"verdict,omitempty"`
	Reject  *Rejection      `json:"rejection,omitempty"`
}

// RunCommand applies (or refuses) one operator command with optimistic
// concurrency: the caller must state the generation it observed. A retry after
// a successful application carries the now-stale generation and is reported as
// a conflict, so commands never execute twice.
func (s *Releaser) RunCommand(ctx context.Context, id string, cmd models.Command, expectedGen int64) (CommandResult, error) {
	switch cmd {
	case models.CmdAdvance, models.CmdPause, models.CmdResume, models.CmdRollback:
	default:
		return CommandResult{}, fmt.Errorf("%w: unknown command %q", ErrBadInput, cmd)
	}

	result := CommandResult{}
	err := s.Store.WithTx(ctx, func(tx store.Tx) error {
		r, err := tx.GetReleaseForUpdate(ctx, id)
		if err != nil {
			return err
		}
		if r.Generation != expectedGen {
			return fmt.Errorf("%w: release at generation %d, command expected %d", store.ErrConflict, r.Generation, expectedGen)
		}

		// Snap to the metrics tick grid so the new observation window tiles
		// whole buckets.
		now := alignWindow(s.Now().UTC())

		// Terminal-state handling.
		if r.State == models.StateRolled {
			// Rolled-back releases stay rolled back. A late healthy verdict
			// arriving now is recorded as evidence but cannot resurrect them.
			v := s.lateHealthyVerdictInTx(ctx, tx, r)
			result.Reject = &Rejection{
				Command: cmd,
				Reason:  "release already rolled back; late healthy metrics cannot resurrect it",
				Verdict: v,
			}
			return nil
		}
		if r.State == models.StateComplete {
			result.Reject = &Rejection{Command: cmd, Reason: "release already complete"}
			return nil
		}

		// Pause/resume do not require a fresh verdict.
		switch cmd {
		case models.CmdPause:
			if r.State == models.StatePaused {
				result.Reject = &Rejection{Command: cmd, Reason: "already paused"}
				return nil
			}
			from := r.Stage
			r.State = models.StatePaused
			r.Generation++
			r.UpdatedAt = now
			if err := tx.UpdateRelease(ctx, r); err != nil {
				return err
			}
			_ = tx.InsertEvent(ctx, models.Event{ReleaseID: id, Generation: r.Generation, Type: "paused", Command: cmd, FromStage: from, CreatedAt: now})
			result.Applied = true
			result.Release = r
			return nil
		case models.CmdResume:
			if r.State != models.StatePaused {
				result.Reject = &Rejection{Command: cmd, Reason: "release is not paused"}
				return nil
			}
			from := r.Stage
			r.State = models.StateActive
			r.StageEnteredAt = now // the observation window restarts after resume
			r.Generation++
			r.UpdatedAt = now
			if err := tx.UpdateRelease(ctx, r); err != nil {
				return err
			}
			_ = tx.InsertEvent(ctx, models.Event{ReleaseID: id, Generation: r.Generation, Type: "resumed", Command: cmd, FromStage: from, ToStage: from, CreatedAt: now})
			result.Applied = true
			result.Release = r
			return nil
		}

		// advance/rollback require an up-to-date verdict.
		if r.State == models.StatePaused {
			result.Reject = &Rejection{Command: cmd, Reason: "release paused: resume before issuing advance/rollback"}
			return nil
		}

		// The evaluator must run inside the transaction with the row locked so
		// no concurrent command can race the decision.
		s.Eval.Now = s.Now
		obs, err := s.Eval.Evaluate(ctx, r)
		if err != nil {
			return err
		}
		obs.Generation = r.Generation
		_ = tx.InsertObservation(ctx, storeObs(obs))
		v := obs.Verdict
		result.Verdict = &v

		switch cmd {
		case models.CmdRollback:
			from := r.Stage
			idx := models.StageIndex(r.Stage)
			r.State = models.StateRolled
			if idx <= 0 {
				r.StageWeight = 0
				r.Stage = models.Stage5 // rolled back to 0% before the first stage
			} else {
				r.Stage = models.Stages[idx-1]
				r.StageWeight = weightOf(r.Stage)
			}
			r.Generation++
			r.UpdatedAt = now
			if err := tx.UpdateRelease(ctx, r); err != nil {
				return err
			}
			_ = tx.InsertEvent(ctx, models.Event{ReleaseID: id, Generation: r.Generation, Type: "rolled_back", Command: cmd, FromStage: from, ToStage: r.Stage, Verdict: &v, Detail: summarize(v), CreatedAt: now})
			result.Applied = true
			result.Release = r
			return nil

		case models.CmdAdvance:
			if !allows(v.Allowed, models.CmdAdvance) {
				_ = tx.InsertEvent(ctx, models.Event{ReleaseID: id, Generation: r.Generation, Type: "rejected", Command: cmd, FromStage: r.Stage, ToStage: r.Stage, Verdict: &v, Detail: summarize(v), CreatedAt: now})
				result.Reject = &Rejection{Command: cmd, Reason: rejectReason(v), Verdict: v}
				result.Release = r
				return nil
			}
			from := r.Stage
			idx := models.StageIndex(r.Stage)
			r.Generation++
			if idx == len(models.Stages)-1 {
				r.State = models.StateComplete
			} else {
				r.Stage = models.Stages[idx+1]
				r.StageWeight = weightOf(r.Stage)
			}
			r.StageEnteredAt = now // fresh observation window for the new stage
			r.UpdatedAt = now
			if err := tx.UpdateRelease(ctx, r); err != nil {
				return err
			}
			etype := "advanced"
			if r.State == models.StateComplete {
				etype = "completed"
			}
			_ = tx.InsertEvent(ctx, models.Event{ReleaseID: id, Generation: r.Generation, Type: etype, Command: cmd, FromStage: from, ToStage: r.Stage, Verdict: &v, Detail: summarize(v), CreatedAt: now})
			result.Applied = true
			result.Release = r
			return nil
		}
		return nil
	})
	if err != nil {
		return CommandResult{}, err
	}
	return result, nil
}

// Probe evaluates the current stage without changing any state. The returned
// verdict is the evidence a client uses before calling advance.
func (s *Releaser) Probe(ctx context.Context, id string) (models.Release, Observation, error) {
	r, err := s.Store.GetRelease(ctx, id)
	if err != nil {
		return r, Observation{}, err
	}
	s.Eval.Now = s.Now
	obs, err := s.Eval.Evaluate(ctx, r)
	if err != nil {
		return r, Observation{}, err
	}
	obs.Generation = r.Generation
	// Persist probe evidence outside of a command transaction.
	_ = s.Store.InsertObservationDirect(ctx, storeObs(obs))
	return r, obs, nil
}

func allows(cs []models.Command, c models.Command) bool {
	for _, x := range cs {
		if x == c {
			return true
		}
	}
	return false
}

func rejectReason(v models.Verdict) string {
	switch v.Health {
	case "degraded":
		return "verdict degraded: " + summarize(v)
	case "unknown":
		return "verdict unknown: " + summarize(v)
	default:
		return "advance not permitted: " + summarize(v)
	}
}

func summarize(v models.Verdict) string {
	m := v.Metrics
	s := fmt.Sprintf("health=%s reasons=%v samples=%d errors=%d buckets=%d/%d",
		v.Health, v.Reasons, m.Samples, m.Errors, m.BucketsCovered, m.BucketsRequired)
	if m.ErrorRateLower != nil && m.ErrorRateUpper != nil {
		s += fmt.Sprintf(" errRate95%%=[%.4f,%.4f]", *m.ErrorRateLower, *m.ErrorRateUpper)
	}
	if m.LatencyLowerMS != nil && m.LatencyUpperMS != nil {
		s += fmt.Sprintf(" latencyMean95%%=[%.1f,%.1f]ms", *m.LatencyLowerMS, *m.LatencyUpperMS)
	}
	return s
}

// lateHealthyVerdictInTx computes a verdict for a rolled-back release and
// records it (marked late/superseded) inside the already-open transaction. It
// is evidence only — the caller never applies it.
func (s *Releaser) lateHealthyVerdictInTx(ctx context.Context, tx store.Tx, r models.Release) models.Verdict {
	s.Eval.Now = s.Now
	obs, err := s.Eval.Evaluate(ctx, r)
	if err == nil {
		obs.Generation = r.Generation
		obs.Superseded = true
		_ = tx.InsertObservation(ctx, storeObs(obs))
		v := obs.Verdict
		v.Late = true
		return v
	}
	return models.Verdict{Health: "unknown", Reasons: []models.Reason{models.ReasonMetricsMissing}, ThresholdV: r.ThresholdVersion}
}
