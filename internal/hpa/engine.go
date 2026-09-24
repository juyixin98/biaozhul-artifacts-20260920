package hpa

import (
	"context"
	"errors"
	"fmt"
	"math"
	"time"
)

// ErrFutureMetric rejects metric timestamps more than futureSkep ahead of the
// server clock: an event from the future is a protocol error, not a sample.
var ErrFutureMetric = errors.New("metricTimestamp is too far in the future")

const futureSkew = 60 * time.Second

// Store is the persistence boundary the engine needs. Every decision runs
// inside RunDecideTx so that last-timestamp checking, recording the raw
// recommendation and reading the stable window are atomic.
type Store interface {
	RunDecideTx(ctx context.Context, scaler string, fn func(tx Tx) error) error
}

// Tx is one decision transaction.
type Tx interface {
	// LastMetricTS returns the greatest event time already accepted.
	LastMetricTS() (time.Time, bool, error)
	// SetLastMetricTS advances the high-water mark. Event-time regressions
	// are refused before this is called.
	SetLastMetricTS(ts time.Time) error
	// InsertRecommendation adds this tick's raw formula output, tagged with
	// the active config version so a config change implicitly starts a new,
	// empty stable window.
	InsertRecommendation(version int64, ts time.Time, rawDesired int) error
	// WindowMax returns the maximum raw recommendation in [from,to] for one
	// config version and how many samples were considered.
	WindowMax(version int64, from, to time.Time) (maxDesired int, samples int, err error)
	// InsertDecision appends the immutable audit row.
	InsertDecision(d *Decision) error
}

// ErrMetricRegression is the event-time ordering guard: a new request whose
// metricTimestamp is older than the latest one already accepted is rejected
// so late events can never overwrite a newer recommendation.
var ErrMetricRegression = errors.New("metricTimestamp is older than the latest accepted metric timestamp")

// Analyze is the pure decision core. It performs no I/O: given config, a
// request and the current (controllable) clock time it returns the raw
// formula analysis. The stable window is applied later by Decide.
func Analyze(cfg Config, req DecisionRequest, now time.Time) Analysis {
	a := Analysis{InstanceDetails: make([]InstanceDetail, 0, len(req.Instances))}

	// --- data quality gates -------------------------------------------------
	if len(req.Instances) == 0 || req.CurrentReplicas == 0 {
		a.Hold = true
		a.HoldReason = ReasonNoInstances
		return a
	}
	if now.Sub(req.MetricTimestamp) > time.Duration(cfg.MetricFreshnessSeconds)*time.Second {
		a.Hold = true
		a.HoldReason = ReasonMetricsStale
		return a
	}

	readyIDs, missingReady := 0, 0
	var reportedSum float64
	for _, in := range req.Instances {
		detail := InstanceDetail{Name: in.Name, Ready: in.Ready, ReportedPct: in.UtilizationPct}
		switch {
		case !in.Ready:
			// Not-ready pods are excluded from the measured average. Their
			// safety weight (0% / 100%) is decided once the direction is known.
			a.UnreadyTotal++
			detail.ExcludedUnready = true
			detail.Note = "not ready: excluded from measured average"
		case in.UtilizationPct == nil:
			// Missing metric: explicitly NOT treated as 0%. Counted and
			// conservatively assumed at 100% in the safety weighting.
			a.ReadyTotal++
			missingReady++
			detail.Missing = true
			detail.EffectivePct = 100
			detail.AssumedForSafety = true
			detail.Note = "metric missing: assumed 100% for safety, never 0"
		default:
			a.ReadyTotal++
			readyIDs++
			reportedSum += *in.UtilizationPct
			detail.EffectivePct = *in.UtilizationPct
			detail.Note = "reported metric used"
		}
		a.InstanceDetails = append(a.InstanceDetails, detail)
	}
	a.ReadyReporting = readyIDs
	a.ReadyMissing = missingReady

	if a.ReadyTotal == 0 {
		a.Hold = true
		a.HoldReason = ReasonNoReadyInstances
		return a
	}
	if readyIDs == 0 {
		// Every ready pod is missing its metric. We must not fall back to
		// zero utilization (which would wrongly scale down): hold and record
		// no recommendation.
		a.Hold = true
		a.HoldReason = ReasonAllReadyMissing
		return a
	}

	meanReported := reportedSum / float64(readyIDs)
	a.ReportedUtilPct = &meanReported
	a.EffectiveUtilPct = meanReported

	// --- HPA formula --------------------------------------------------------
	// Ratio of measured utilization to target, and the tolerance band.
	ratio := meanReported / float64(cfg.TargetUtilization)
	a.Ratio = ratio
	tol := cfg.TolerancePct / 100.0

	// Epsilon covers binary-float boundaries (e.g. 77/70 vs 10% exactly).
	if math.Abs(ratio-1) <= tol+1e-9 {
		// Inside [1-tol, 1+tol]: keep the current count. This is what stops
		// oscillation around the target.
		a.WithinTolerance = true
		a.RawDesired = req.CurrentReplicas
		a.RawAction = Hold
		a.RawReason = ReasonToleranceHold
		if ratio == 1 && cfg.TolerancePct == 0 {
			a.RawReason = ReasonNoChange
		}
		return a
	}

	scalingUp := ratio > 1
	if scalingUp {
		a.RawReason = ReasonRawScaleUp
	} else {
		a.RawReason = ReasonRawScaleDown
	}

	// Safety weighting (Kubernetes HPA semantics):
	//   - scaling up : not-ready pods count at 100%
	//   - scaling down: not-ready pods count at 0%
	//   - ready pod with a missing metric always counts at 100%
	var effectiveSum float64
	for i := range a.InstanceDetails {
		d := &a.InstanceDetails[i]
		switch {
		case d.Missing:
			effectiveSum += 100
		case d.ExcludedUnready:
			if scalingUp {
				d.EffectivePct = 100
				d.AssumedForSafety = true
				d.Note = "not ready during scale-up: assumed 100% for safety"
			} else {
				d.EffectivePct = 0
				d.AssumedForSafety = true
				d.Note = "not ready during scale-down: assumed 0% (no scale-up on its behalf)"
			}
			effectiveSum += d.EffectivePct
		default:
			effectiveSum += d.EffectivePct
		}
	}
	eff := effectiveSum / float64(req.CurrentReplicas)
	a.EffectiveUtilPct = eff

	// desiredReplicas = ceil(current * effectiveUtilization / targetUtilization)
	raw := ceilEPS(float64(req.CurrentReplicas) * eff / float64(cfg.TargetUtilization))
	a.RawDesired = raw

	switch {
	case raw < cfg.MinReplicas:
		a.RawDesired = cfg.MinReplicas
		a.RawCappedMin = true
	case raw > cfg.MaxReplicas:
		a.RawDesired = cfg.MaxReplicas
		a.RawCappedMax = true
	}
	a.RawAction = actionOf(a.RawDesired, req.CurrentReplicas)
	return a
}

// Decide runs one decision end to end: it checks event-time ordering,
// analyzes the raw formula output, records that output tagged with the config
// version, applies the stable window (maximum recommendation inside it),
// clamps to min/max and finally signs the result.
func (s *Service) Decide(ctx context.Context, cfg Config, req DecisionRequest, now time.Time) (*Decision, error) {
	if err := Validate(&cfg); err != nil {
		return nil, fmt.Errorf("config: %w", err)
	}
	if req.ScalerID != "" && req.ScalerID != cfg.ScalerID {
		return nil, fmt.Errorf("scalerId mismatch: %w", ErrEmptyScalerID)
	}
	if req.MetricTimestamp.IsZero() {
		return nil, ErrZeroMetricTimestamp
	}

	analysis := Analyze(cfg, req, now)

	d := &Decision{
		ScalerID:          cfg.ScalerID,
		ConfigVersion:     cfg.Version,
		ConfigFingerprint: cfg.Fingerprint,
		MetricTimestamp:   req.MetricTimestamp.UTC(),
		DecidedAt:         now.UTC(),
		CurrentReplicas:   req.CurrentReplicas,
		ReadyTotal:        analysis.ReadyTotal,
		UnreadyTotal:      analysis.UnreadyTotal,
		ReadyReporting:    analysis.ReadyReporting,
		ReadyMissing:      analysis.ReadyMissing,
		EffectiveUtilPct:  round4(analysis.EffectiveUtilPct),
		WithinTolerance:   analysis.WithinTolerance,
		InstanceDetails:   analysis.InstanceDetails,
		FinalReasons:      []string{},
	}
	if analysis.ReportedUtilPct != nil {
		v := round4(*analysis.ReportedUtilPct)
		d.ReportedUtilPct = &v
	}

	err := s.store.RunDecideTx(ctx, cfg.ScalerID, func(tx Tx) error {
		// Event-time monotonicity is the highest-integrity guard: it must
		// hold even for a malformed late request, before any body validation
		// that would reject the call for a different reason.
		if last, ok, err := tx.LastMetricTS(); err != nil {
			return err
		} else if ok && req.MetricTimestamp.Before(last) {
			return ErrMetricRegression
		}

		// Request-shape validation runs once the event ordering is known.
		if req.CurrentReplicas < 0 {
			return ErrNegativeReplicas
		}
		if len(req.Instances) != req.CurrentReplicas {
			return ErrInstanceCountMismatch
		}
		if req.MetricTimestamp.After(now.Add(futureSkew)) {
			return ErrFutureMetric
		}

		if err := tx.SetLastMetricTS(req.MetricTimestamp); err != nil {
			return err
		}

		if analysis.Hold {
			// Insufficient data: no formula output, nothing added to the
			// window. Keep the current fleet and report why.
			d.RawDesired = req.CurrentReplicas
			d.RawAction = Hold
			d.RawReason = analysis.HoldReason
			d.WindowedDesired = req.CurrentReplicas
			d.FinalDesired = req.CurrentReplicas
			d.FinalAction = Hold
			d.FinalReasons = append(d.FinalReasons, analysis.HoldReason)
			d.RecommendationRecorded = false
			d.Signature = SignDecision(s.key, d)
			return tx.InsertDecision(d)
		}

		if err := tx.InsertRecommendation(cfg.Version, req.MetricTimestamp, analysis.RawDesired); err != nil {
			return err
		}
		d.RecommendationRecorded = true

		// Choose which stable window applies. A tolerance hold is treated
		// conservatively like a potential scale-down (the longer window).
		var windowSec int
		switch analysis.RawAction {
		case ScaleUp:
			windowSec = cfg.ScaleUpStabilizationWindowSeconds
		default:
			windowSec = cfg.ScaleDownStabilizationWindowSeconds
		}
		from := req.MetricTimestamp.Add(-time.Duration(windowSec) * time.Second)
		maxRaw, samples, err := tx.WindowMax(cfg.Version, from, req.MetricTimestamp)
		if err != nil {
			return err
		}
		d.RawDesired = analysis.RawDesired
		d.RawAction = analysis.RawAction
		d.RawReason = analysis.RawReason
		// Raw-level clamping is itself part of the audit trail.
		if analysis.RawCappedMax {
			d.FinalReasons = appendUnique(d.FinalReasons, ReasonCappedAtMax)
		}
		if analysis.RawCappedMin {
			d.FinalReasons = appendUnique(d.FinalReasons, ReasonCappedAtMin)
		}
		d.WindowedDesired = maxRaw
		d.WindowSeconds = windowSec
		d.WindowSamples = samples
		d.FinalDesired = maxRaw

		// Stable-window rationale.
		if windowSec == 0 {
			d.FinalReasons = append(d.FinalReasons, analysis.RawReason, ReasonNoWindow)
		} else if analysis.RawAction == ScaleUp {
			d.FinalReasons = append(d.FinalReasons, analysis.RawReason, ReasonScaleUpWindowMax)
		} else {
			d.FinalReasons = append(d.FinalReasons, analysis.RawReason, ReasonScaleDownWindowMax)
		}

		// Final clamp to the configured replica range.
		switch {
		case d.FinalDesired < cfg.MinReplicas:
			d.FinalDesired = cfg.MinReplicas
			d.FinalCappedMin = true
			d.FinalReasons = appendUnique(d.FinalReasons, ReasonCappedAtMin)
		case d.FinalDesired > cfg.MaxReplicas:
			d.FinalDesired = cfg.MaxReplicas
			d.FinalCappedMax = true
			d.FinalReasons = appendUnique(d.FinalReasons, ReasonCappedAtMax)
		}
		d.FinalAction = actionOf(d.FinalDesired, req.CurrentReplicas)
		d.Signature = SignDecision(s.key, d)
		return tx.InsertDecision(d)
	})
	if err != nil {
		return nil, err
	}
	return d, nil
}

// Service wires the engine to its store and signing key.
type Service struct {
	store Store
	key   []byte
}

func NewService(store Store, signingKey []byte) *Service {
	return &Service{store: store, key: signingKey}
}

func actionOf(desired, current int) Action {
	switch {
	case desired > current:
		return ScaleUp
	case desired < current:
		return ScaleDown
	default:
		return Hold
	}
}

func ceilEPS(x float64) int {
	return int(math.Ceil(x - 1e-9))
}

func round4(x float64) float64 {
	return math.Round(x*1e4) / 1e4
}

func appendUnique(xs []string, v string) []string {
	for _, x := range xs {
		if x == v {
			return xs
		}
	}
	return append(xs, v)
}
