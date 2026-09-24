package eval

import (
	"context"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/example/rollout/internal/metrics"
	"github.com/example/rollout/internal/metricstub"
	"github.com/example/rollout/internal/models"
	"github.com/example/rollout/internal/store"
)

type svcEnv struct {
	st     *store.Memory
	rel    *Releaser
	server *httptest.Server
}

func newSvcEnv(t *testing.T) *svcEnv {
	t.Helper()
	srv := httptest.NewServer(metricstub.Handler())
	t.Cleanup(srv.Close)
	st := store.NewMemory()
	ev := New(metrics.NewClient(srv.URL))
	return &svcEnv{st: st, rel: NewReleaser(st, ev), server: srv}
}

func waitWindow() { time.Sleep(520 * time.Millisecond) }

// TestFullLifecycle walks a healthy release 5% -> 20% -> 50% -> 100% ->
// complete. Each advance must carry the generation it observed and must cite
// a healthy verdict.
func TestFullLifecycle(t *testing.T) {
	if testing.Short() {
		t.Skip("multi-window lifecycle test")
	}
	env := newSvcEnv(t)
	ctx := context.Background()

	r, err := env.rel.CreateRelease(ctx, CreateReleaseInput{
		Name: "checkout", Version: "v1", Scenario: "healthy",
		ObservationMS: 400, MinSamples: 80,
	}, 400, 80, env.server.URL)
	if err != nil {
		t.Fatal(err)
	}
	if r.ThresholdVersion != 1 || r.Stage != models.Stage5 || r.Generation != 0 {
		t.Fatalf("bad initial release: %+v", r)
	}

	want := []struct {
		stage  models.Stage
		weight float64
	}{
		{models.Stage20, 0.20},
		{models.Stage50, 0.50},
		{models.Stage100, 1.00},
	}
	for i, w := range want {
		waitWindow()
		res, err := env.rel.RunCommand(ctx, r.ID, models.CmdAdvance, r.Generation)
		if err != nil {
			t.Fatalf("advance %d: %v", i, err)
		}
		if !res.Applied {
			t.Fatalf("advance %d rejected: %s", i, res.Reject.Reason)
		}
		if res.Release.Stage != w.stage || res.Release.StageWeight != w.weight {
			t.Fatalf("advance %d: got stage=%s weight=%.2f", i, res.Release.Stage, res.Release.StageWeight)
		}
		if res.Verdict == nil || res.Verdict.Health != "healthy" {
			t.Fatalf("advance %d missing healthy verdict: %+v", i, res.Verdict)
		}
		if res.Release.Generation != int64(i+1) {
			t.Fatalf("advance %d generation=%d want %d", i, res.Release.Generation, i+1)
		}
		r = res.Release
	}

	// The final advance at 100% completes the rollout.
	waitWindow()
	res, err := env.rel.RunCommand(ctx, r.ID, models.CmdAdvance, r.Generation)
	if err != nil {
		t.Fatal(err)
	}
	if !res.Applied || res.Release.State != models.StateComplete {
		t.Fatalf("expected completion, applied=%v state=%s", res.Applied, res.Release.State)
	}
}

// TestAdvanceBeforeWindowIsUnknown: a command issued before the observation
// window completes is rejected with an unknown verdict.
func TestAdvanceBeforeWindowIsUnknown(t *testing.T) {
	env := newSvcEnv(t)
	r, err := env.rel.CreateRelease(context.Background(), CreateReleaseInput{
		Name: "svc", Version: "v", Scenario: "healthy", ObservationMS: 2000, MinSamples: 80,
	}, 2000, 80, env.server.URL)
	if err != nil {
		t.Fatal(err)
	}
	res, err := env.rel.RunCommand(context.Background(), r.ID, models.CmdAdvance, 0)
	if err != nil {
		t.Fatal(err)
	}
	if res.Applied {
		t.Fatal("advance should not be applied before window completes")
	}
	if res.Reject.Verdict.Health != "unknown" {
		t.Fatalf("want unknown verdict on early advance, got %s", res.Reject.Verdict.Health)
	}
	if r2, _ := env.st.GetRelease(context.Background(), r.ID); r2.Generation != 0 {
		t.Fatalf("rejected command must not bump generation, got %d", r2.Generation)
	}
}

// TestDegradedBlocksAdvanceButAllowsRollback.
func TestDegradedBlocksAdvance(t *testing.T) {
	env := newSvcEnv(t)
	r, err := env.rel.CreateRelease(context.Background(), CreateReleaseInput{
		Name: "svc", Version: "v", Scenario: "degraded", ObservationMS: 400, MinSamples: 80,
	}, 400, 80, env.server.URL)
	if err != nil {
		t.Fatal(err)
	}
	waitWindow()
	res, err := env.rel.RunCommand(context.Background(), r.ID, models.CmdAdvance, 0)
	if err != nil {
		t.Fatal(err)
	}
	if res.Applied {
		t.Fatal("advance must be blocked on degraded metrics")
	}
	if res.Reject.Verdict.Health != "degraded" {
		t.Fatalf("want degraded, got %s", res.Reject.Verdict.Health)
	}
}

// TestStaleGenerationRejected: the advance is applied at gen 0 -> gen 1; a
// retry that still claims gen 0 must conflict and never execute twice.
func TestStaleGenerationRejected(t *testing.T) {
	if testing.Short() {
		t.Skip("multi-window")
	}
	env := newSvcEnv(t)
	ctx := context.Background()
	r, err := env.rel.CreateRelease(ctx, CreateReleaseInput{
		Name: "svc", Version: "v", Scenario: "healthy", ObservationMS: 400, MinSamples: 80,
	}, 400, 80, env.server.URL)
	if err != nil {
		t.Fatal(err)
	}
	waitWindow()
	first, err := env.rel.RunCommand(ctx, r.ID, models.CmdAdvance, 0)
	if err != nil || !first.Applied {
		t.Fatalf("first advance failed: %v %+v", err, first)
	}
	if first.Release.Generation != 1 {
		t.Fatalf("expected gen 1, got %d", first.Release.Generation)
	}

	// Retry with the stale generation 0 — must conflict, not re-execute.
	retry, err := env.rel.RunCommand(ctx, r.ID, models.CmdAdvance, 0)
	if err == nil {
		t.Fatalf("expected conflict on stale generation, got applied=%v", retry.Applied)
	}
	got, _ := env.st.GetRelease(ctx, r.ID)
	if got.Generation != 1 || got.Stage != models.Stage20 {
		t.Fatalf("retry changed state: gen=%d stage=%s", got.Generation, got.Stage)
	}
}

// TestThresholdFrozenAtStart: changing the global policy after a release has
// started must not change the release's frozen snapshot.
func TestThresholdFrozenAtStart(t *testing.T) {
	env := newSvcEnv(t)
	ctx := context.Background()
	r, err := env.rel.CreateRelease(ctx, CreateReleaseInput{
		Name: "svc", Version: "v", Scenario: "healthy", ObservationMS: 400, MinSamples: 80,
	}, 400, 80, env.server.URL)
	if err != nil {
		t.Fatal(err)
	}
	if r.ThresholdSpec.ErrorRateUpper != 0.05 {
		t.Fatalf("default policy wrong: %+v", r.ThresholdSpec)
	}
	// Tighten the policy globally.
	newT, err := env.st.CreateThreshold(ctx, models.ThresholdSpec{
		ErrorRateUpper: 0.01, LatencyMeanMS: 50, LatencyP95MS: 100,
	}, "tighten")
	if err != nil {
		t.Fatal(err)
	}
	got, _ := env.st.GetRelease(ctx, r.ID)
	if got.ThresholdVersion != 1 {
		t.Fatalf("release threshold version must stay frozen at 1, got %d", got.ThresholdVersion)
	}
	if got.ThresholdSpec.ErrorRateUpper != 0.05 {
		t.Fatalf("release snapshot must not change; got %+v", got.ThresholdSpec)
	}
	// A brand-new release picks up v2.
	r2, err := env.rel.CreateRelease(ctx, CreateReleaseInput{
		Name: "svc2", Version: "v", Scenario: "healthy", ObservationMS: 400, MinSamples: 80,
	}, 400, 80, env.server.URL)
	if err != nil {
		t.Fatal(err)
	}
	if r2.ThresholdVersion != newT.Version || r2.ThresholdSpec.ErrorRateUpper != 0.01 {
		t.Fatalf("new release must use v2 policy, got %d %+v", r2.ThresholdVersion, r2.ThresholdSpec)
	}
}

// TestRollbackThenLateSuccess: after a rollback the release is terminal; even
// a subsequent healthy verdict ("the metrics recovered") cannot resurrect it.
func TestRollbackThenLateSuccess(t *testing.T) {
	if testing.Short() {
		t.Skip("multi-window")
	}
	env := newSvcEnv(t)
	ctx := context.Background()
	r, err := env.rel.CreateRelease(ctx, CreateReleaseInput{
		Name: "svc", Version: "v", Scenario: "degraded", ObservationMS: 400, MinSamples: 80,
	}, 400, 80, env.server.URL)
	if err != nil {
		t.Fatal(err)
	}
	waitWindow()
	rb, err := env.rel.RunCommand(ctx, r.ID, models.CmdRollback, 0)
	if err != nil || !rb.Applied {
		t.Fatalf("rollback failed: %v %+v", err, rb)
	}
	if rb.Release.State != models.StateRolled {
		t.Fatalf("want rolled_back, got %s", rb.Release.State)
	}

	// Even issuing an advance now (the stub is still degraded, but suppose the
	// operator retried later) must be rejected: the release stays rolled back.
	again, err := env.rel.RunCommand(ctx, r.ID, models.CmdAdvance, rb.Release.Generation)
	if err != nil {
		t.Fatal(err)
	}
	if again.Applied {
		t.Fatal("a rolled-back release must not accept commands")
	}
	if again.Reject == nil || again.Reject.Verdict.Late != true {
		t.Fatalf("rejection must carry a late verdict, got %+v", again.Reject)
	}
	got, _ := env.st.GetRelease(ctx, r.ID)
	if got.State != models.StateRolled {
		t.Fatalf("state changed to %s", got.State)
	}
}

// TestPauseResumeRestartsWindow: pausing bumps the generation; resuming
// restarts the observation window, so an immediate advance is unknown again.
func TestPauseResumeRestartsWindow(t *testing.T) {
	env := newSvcEnv(t)
	ctx := context.Background()
	r, err := env.rel.CreateRelease(ctx, CreateReleaseInput{
		Name: "svc", Version: "v", Scenario: "healthy", ObservationMS: 400, MinSamples: 80,
	}, 400, 80, env.server.URL)
	if err != nil {
		t.Fatal(err)
	}
	p, err := env.rel.RunCommand(ctx, r.ID, models.CmdPause, 0)
	if err != nil || !p.Applied || p.Release.State != models.StatePaused || p.Release.Generation != 1 {
		t.Fatalf("pause failed: %v %+v", err, p)
	}
	// Advance while paused must be refused.
	blocked, _ := env.rel.RunCommand(ctx, r.ID, models.CmdAdvance, 1)
	if blocked.Applied {
		t.Fatal("advance while paused must be refused")
	}
	res, err := env.rel.RunCommand(ctx, r.ID, models.CmdResume, 1)
	if err != nil || !res.Applied || res.Release.Generation != 2 {
		t.Fatalf("resume failed: %v %+v", err, res)
	}
	// Fresh window: immediate advance is unknown.
	early, _ := env.rel.RunCommand(ctx, r.ID, models.CmdAdvance, 2)
	if early.Applied || early.Reject.Verdict.Health != "unknown" {
		t.Fatalf("advance right after resume must be unknown, applied=%v", early.Applied)
	}
}
