// Package eval contains the decision engine: it turns raw metrics pulled for a
// release's current-stage observation window into a cited verdict.
package eval

import (
	"context"
	"math"
	"time"

	"github.com/example/rollout/internal/metrics"
	"github.com/example/rollout/internal/models"
	"github.com/example/rollout/internal/stats"
)

// Fetcher is the slice of the metrics client the engine depends on.
type Fetcher interface {
	Fetch(ctx context.Context, releaseID, scenario string, from, to, createdAt time.Time) (*metrics.Window, error)
}

// Evaluator evaluates releases against frozen thresholds.
type Evaluator struct {
	Fetcher Fetcher
	Now     func() time.Time
}

func New(f Fetcher) *Evaluator {
	return &Evaluator{Fetcher: f, Now: time.Now}
}

// Observation is the result of evaluating one window. It is persisted and also
// returned by the probe API.
type Observation struct {
	ReleaseID   string
	Stage       models.Stage
	Generation  int64
	WindowStart time.Time
	WindowEnd   time.Time
	Verdict     models.Verdict
	Superseded  bool
}

// Evaluate computes the verdict for the release's current stage. It performs a
// real HTTP call, aggregates raw samples, and computes two 95% confidence
// intervals (Wilson for the error rate, Student-t for the latency mean).
func (e *Evaluator) Evaluate(ctx context.Context, r models.Release) (Observation, error) {
	now := e.Now()
	from := r.StageEnteredAt
	to := from.Add(time.Duration(r.ObservationMS) * time.Millisecond)

	obs := Observation{
		ReleaseID:   r.ID,
		Stage:       r.Stage,
		WindowStart: from,
		WindowEnd:   to,
	}
	v := models.Verdict{
		ThresholdV:  r.ThresholdVersion,
		Reasons:     []models.Reason{},
		EvaluatedAt: now,
		Metrics: models.MetricResult{
			WindowStart: from.UTC().Format(time.RFC3339Nano),
			WindowEnd:   to.UTC().Format(time.RFC3339Nano),
		},
	}

	// 1) The observation window must have fully elapsed. A short window is
	//    unknown — never presumed healthy.
	if now.Before(to) {
		v.Health = "unknown"
		v.Reasons = append(v.Reasons, models.ReasonWindowIncomplete)
		v.Allowed = []models.Command{}
		v.Metrics.BucketsRequired = requiredBuckets(r.ObservationMS, metrics.TickMS)
		obs.Verdict = v
		return obs, nil
	}

	// 2) Pull metrics over real HTTP.
	win, err := e.Fetcher.Fetch(ctx, r.ID, r.Scenario, from, to, r.CreatedAt)
	if err != nil {
		if _, ok := err.(*metrics.ErrUnavailable); ok {
			v.Health = "unknown"
			v.Reasons = append(v.Reasons, models.ReasonMetricsMissing)
			v.Allowed = []models.Command{models.CmdPause, models.CmdRollback}
			v.Metrics.BucketsRequired = requiredBuckets(r.ObservationMS, metrics.TickMS)
			obs.Verdict = v
			return obs, nil
		}
		return obs, err
	}

	tickMS := win.TickMS
	required := requiredBuckets(r.ObservationMS, tickMS)
	v.Metrics.BucketsRequired = required
	v.Metrics.BucketsCovered = len(win.Buckets)

	if !win.Available {
		v.Health = "unknown"
		v.Reasons = append(v.Reasons, models.ReasonMetricsMissing)
		v.Allowed = []models.Command{models.CmdPause, models.CmdRollback}
		obs.Verdict = v
		return obs, nil
	}

	// 3) Coverage: the returned buckets must tile the complete window.
	if len(win.Buckets) < required {
		v.Health = "unknown"
		v.Reasons = append(v.Reasons, models.ReasonCoverageIncomplete)
		v.Allowed = []models.Command{models.CmdPause, models.CmdRollback}
		obs.Verdict = v
		return obs, nil
	}

	// 4) Aggregate raw samples.
	var totalSamples, totalErrors int64
	var latencies []float64
	for _, b := range win.Buckets {
		totalSamples += b.Samples
		totalErrors += b.Errors
		latencies = append(latencies, b.LatencyMS...)
	}
	v.Metrics.Samples = totalSamples
	v.Metrics.Errors = totalErrors
	if totalSamples > 0 {
		point := float64(totalErrors) / float64(totalSamples)
		v.Metrics.ErrorRatePoint = point
	}

	// 5) Minimum sample gate. Insufficient samples -> unknown.
	if totalSamples < r.MinSamples {
		v.Health = "unknown"
		v.Reasons = append(v.Reasons, models.ReasonSamplesInsufficient)
		// Still attach any intervals we can compute, as evidence.
		e.attachIntervals(&v, totalErrors, totalSamples, latencies)
		v.Allowed = []models.Command{models.CmdPause, models.CmdRollback}
		obs.Verdict = v
		return obs, nil
	}

	// 6) Real confidence-interval evidence.
	e.attachIntervals(&v, totalErrors, totalSamples, latencies)

	// 7) Threshold comparison against the FROZEN policy.
	degraded := false
	if v.Metrics.ErrorRateUpper != nil && *v.Metrics.ErrorRateUpper > r.ThresholdSpec.ErrorRateUpper {
		v.Reasons = append(v.Reasons, models.ReasonErrorRateBreached)
		degraded = true
	}
	if v.Metrics.LatencyUpperMS != nil && *v.Metrics.LatencyUpperMS > r.ThresholdSpec.LatencyMeanMS {
		v.Reasons = append(v.Reasons, models.ReasonLatencyBreached)
		degraded = true
	}

	switch {
	case degraded:
		v.Health = "degraded"
		// A degraded verdict never permits advancing; rollback is the safe path.
		v.Allowed = []models.Command{models.CmdPause, models.CmdRollback}
	default:
		v.Health = "healthy"
		v.Reasons = append(v.Reasons, models.ReasonOK)
		// At the last stage "advance" means finish the rollout (complete).
		v.Allowed = []models.Command{models.CmdAdvance, models.CmdPause, models.CmdRollback}
	}

	obs.Verdict = v
	return obs, nil
}

func (e *Evaluator) attachIntervals(v *models.Verdict, errors, n int64, latencies []float64) {
	if n > 0 {
		if lo, hi, err := stats.WilsonInterval(errors, n); err == nil {
			v.Metrics.ErrorRateLower = &lo
			v.Metrics.ErrorRateUpper = &hi
		}
	}
	if len(latencies) >= 2 {
		if mi, err := stats.MeanTInterval(latencies); err == nil {
			mean := mi.Mean
			up := mi.Upper
			lo := mi.Lower
			sd := mi.StdDev
			v.Metrics.LatencyMeanMS = &mean
			v.Metrics.LatencyUpperMS = &up
			v.Metrics.LatencyLowerMS = &lo
			v.Metrics.LatencyStddevMS = &sd
		}
	}
}

// requiredBuckets is observation_ms / tick width.
func requiredBuckets(observationMS, tickMS int64) int {
	return int(math.Round(float64(observationMS) / float64(tickMS)))
}
