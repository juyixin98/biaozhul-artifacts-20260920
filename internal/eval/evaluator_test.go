package eval

import (
	"context"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/example/rollout/internal/metrics"
	"github.com/example/rollout/internal/metricstub"
	"github.com/example/rollout/internal/models"
)

// testEnv wires a real in-process metrics stub over real (loopback) HTTP to a
// real evaluator. Nothing about the metrics path is mocked.
type testEnv struct {
	evaluator *Evaluator
	server    *httptest.Server
}

func newTestEnv() *testEnv {
	srv := httptest.NewServer(metricstub.Handler())
	c := metrics.NewClient(srv.URL)
	return &testEnv{evaluator: New(c), server: srv}
}

func (e *testEnv) close() { e.server.Close() }

func makeRelease(scenario string, obsMS, minSamples int64, age time.Duration) models.Release {
	// Align to a 200ms tick boundary so the stub's bucket-index math lines up
	// with scenario phase boundaries.
	now := time.Now().UTC().Truncate(200 * time.Millisecond)
	return models.Release{
		ID:               "rel_test",
		Name:             "svc",
		Version:          "1.2.3",
		State:            models.StateActive,
		Stage:            models.Stage5,
		StageWeight:      0.05,
		Generation:       0,
		ObservationMS:    obsMS,
		MinSamples:       minSamples,
		ThresholdVersion: 1,
		ThresholdSpec:    models.DefaultThresholdSpec(),
		Scenario:         scenario,
		// The stub anchors scenario time to CreatedAt; the observation window
		// starts at StageEnteredAt. For a stage entered immediately after
		// creation these are the same instant.
		StageEnteredAt: now.Add(-age),
		CreatedAt:      now.Add(-age),
	}
}

func hasReason(rs []models.Reason, want models.Reason) bool {
	for _, r := range rs {
		if r == want {
			return true
		}
	}
	return false
}

// TestHealthyWindowPasses: full window, ample samples, zero errors, latency
// mean well under the limit => healthy and advance allowed.
func TestHealthyWindowPasses(t *testing.T) {
	env := newTestEnv()
	defer env.close()

	r := makeRelease("healthy", 400, 80, 450*time.Millisecond)
	obs, err := env.evaluator.Evaluate(context.Background(), r)
	if err != nil {
		t.Fatal(err)
	}
	if obs.Verdict.Health != "healthy" {
		t.Fatalf("want healthy, got %s reasons=%v summary=%s", obs.Verdict.Health, obs.Verdict.Reasons, summarize(obs.Verdict))
	}
	if !containsCmd(obs.Verdict.Allowed, models.CmdAdvance) {
		t.Fatalf("advance not allowed: %v", obs.Verdict.Allowed)
	}
	m := obs.Verdict.Metrics
	if m.Samples < 80 {
		t.Fatalf("samples=%d want>=80", m.Samples)
	}
	if m.ErrorRateUpper == nil || m.LatencyUpperMS == nil {
		t.Fatalf("missing interval evidence: %+v", m)
	}
	if *m.ErrorRateUpper >= r.ThresholdSpec.ErrorRateUpper {
		t.Fatalf("error rate upper %f should be under %f", *m.ErrorRateUpper, r.ThresholdSpec.ErrorRateUpper)
	}
	if *m.LatencyUpperMS >= r.ThresholdSpec.LatencyMeanMS {
		t.Fatalf("latency upper %f should be under %f", *m.LatencyUpperMS, r.ThresholdSpec.LatencyMeanMS)
	}
}

// TestWindowNotCompleteIsUnknown: time since stage entry < observation window.
func TestWindowNotCompleteIsUnknown(t *testing.T) {
	env := newTestEnv()
	defer env.close()

	r := makeRelease("healthy", 2000, 80, 100*time.Millisecond)
	obs, err := env.evaluator.Evaluate(context.Background(), r)
	if err != nil {
		t.Fatal(err)
	}
	if obs.Verdict.Health != "unknown" || !hasReason(obs.Verdict.Reasons, models.ReasonWindowIncomplete) {
		t.Fatalf("want unknown/window_incomplete, got %s %v", obs.Verdict.Health, obs.Verdict.Reasons)
	}
	if containsCmd(obs.Verdict.Allowed, models.CmdAdvance) {
		t.Fatal("advance must not be allowed before the window completes")
	}
}

// TestPersistentDegradation: the degraded scenario breaches both metrics for
// the whole window => degraded, advance forbidden.
func TestPersistentDegradation(t *testing.T) {
	env := newTestEnv()
	defer env.close()

	r := makeRelease("degraded", 400, 80, 450*time.Millisecond)
	obs, err := env.evaluator.Evaluate(context.Background(), r)
	if err != nil {
		t.Fatal(err)
	}
	if obs.Verdict.Health != "degraded" {
		t.Fatalf("want degraded, got %s reasons=%v summary=%s", obs.Verdict.Health, obs.Verdict.Reasons, summarize(obs.Verdict))
	}
	if containsCmd(obs.Verdict.Allowed, models.CmdAdvance) {
		t.Fatal("advance must be forbidden while degraded")
	}
	if !hasReason(obs.Verdict.Reasons, models.ReasonErrorRateBreached) ||
		!hasReason(obs.Verdict.Reasons, models.ReasonLatencyBreached) {
		t.Fatalf("want both breaches, got %v", obs.Verdict.Reasons)
	}
	if *obs.Verdict.Metrics.ErrorRateUpper <= r.ThresholdSpec.ErrorRateUpper {
		t.Fatal("error-rate upper bound must exceed threshold")
	}
}

// TestInsufficientSamplesIsUnknown: buckets exist and window completed but the
// sample count is below the stage minimum.
func TestInsufficientSamplesIsUnknown(t *testing.T) {
	env := newTestEnv()
	defer env.close()

	r := makeRelease("insufficient", 400, 80, 450*time.Millisecond)
	obs, err := env.evaluator.Evaluate(context.Background(), r)
	if err != nil {
		t.Fatal(err)
	}
	if obs.Verdict.Health != "unknown" || !hasReason(obs.Verdict.Reasons, models.ReasonSamplesInsufficient) {
		t.Fatalf("want unknown/samples_insufficient, got %s %v summary=%s", obs.Verdict.Health, obs.Verdict.Reasons, summarize(obs.Verdict))
	}
	if obs.Verdict.Metrics.Samples >= r.MinSamples {
		t.Fatalf("samples=%d should be below minimum %d", obs.Verdict.Metrics.Samples, r.MinSamples)
	}
}

// TestBlackoutIsUnknown: a total metrics outage is unknown, never healthy.
func TestBlackoutIsUnknown(t *testing.T) {
	env := newTestEnv()
	defer env.close()

	r := makeRelease("blackout", 400, 80, 450*time.Millisecond)
	obs, err := env.evaluator.Evaluate(context.Background(), r)
	if err != nil {
		t.Fatal(err)
	}
	if obs.Verdict.Health != "unknown" || !hasReason(obs.Verdict.Reasons, models.ReasonMetricsMissing) {
		t.Fatalf("want unknown/metrics_missing, got %s %v", obs.Verdict.Health, obs.Verdict.Reasons)
	}
}

// TestShortSpikeInSmallWindow: a burst fully inside a short observation window
// flips the verdict to degraded (evidence upper bounds cross the thresholds).
func TestShortSpikeInSmallWindow(t *testing.T) {
	env := newTestEnv()
	defer env.close()

	// Model a stage entered at elapsed 1.0s of a spike release (created 1.8s
	// ago). The stub's scenario clock is anchored at CreatedAt, while the
	// observation window starts at StageEnteredAt. The 800ms window therefore
	// covers elapsed 1.0s..1.8s = 4 burst buckets, and is fully in the past.
	now := time.Now().UTC().Truncate(200 * time.Millisecond)
	created := now.Add(-1800 * time.Millisecond)
	entered := now.Add(-800 * time.Millisecond) // elapsed 1.0s
	r := models.Release{
		ID: "rel_spike", Name: "svc", Version: "v", State: models.StateActive,
		Stage: models.Stage20, StageWeight: 0.20, ObservationMS: 800, MinSamples: 80,
		ThresholdVersion: 1, ThresholdSpec: models.DefaultThresholdSpec(),
		Scenario: "spike", StageEnteredAt: entered, CreatedAt: created,
	}
	obs, err := env.evaluator.Evaluate(context.Background(), r)
	if err != nil {
		t.Fatal(err)
	}
	if obs.Verdict.Health != "degraded" {
		t.Fatalf("spike window should be degraded, got %s reasons=%v summary=%s",
			obs.Verdict.Health, obs.Verdict.Reasons, summarize(obs.Verdict))
	}
}

// TestSpikeDilutedAcrossLongWindow: the same burst spread over a longer,
// complete window is statistically consistent with healthy — this is the
// transient-spike behavior the interval test is meant to capture.
func TestSpikeDilutedAcrossLongWindow(t *testing.T) {
	if testing.Short() {
		t.Skip("long window")
	}
	env := newTestEnv()
	defer env.close()

	// Release created 4.0s ago; the window is the full first 4.0s. The burst
	// is 10 of 20 buckets.
	t0 := time.Now().UTC().Add(-4000 * time.Millisecond).Truncate(200 * time.Millisecond)
	r := models.Release{
		ID: "rel_diluted", Name: "svc", Version: "v", State: models.StateActive,
		Stage: models.Stage5, StageWeight: 0.05, ObservationMS: 4000, MinSamples: 80,
		ThresholdVersion: 1, ThresholdSpec: models.DefaultThresholdSpec(),
		Scenario: "spike", StageEnteredAt: t0, CreatedAt: t0,
	}
	obs, err := env.evaluator.Evaluate(context.Background(), r)
	if err != nil {
		t.Fatal(err)
	}
	if obs.Verdict.Health != "healthy" {
		t.Fatalf("diluted spike should be healthy, got %s reasons=%v summary=%s",
			obs.Verdict.Health, obs.Verdict.Reasons, summarize(obs.Verdict))
	}
}

func containsCmd(cs []models.Command, c models.Command) bool {
	for _, x := range cs {
		if x == c {
			return true
		}
	}
	return false
}
