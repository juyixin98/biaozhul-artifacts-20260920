package store

import (
	"context"
	"encoding/json"
	"os"
	"testing"
	"time"

	"github.com/example/rollout/internal/models"
)

// pgTestURL is read from ROLLOUT_TEST_DATABASE_URL. The test is skipped when it
// is unset, so `go test ./...` stays green without a database; the acceptance
// script sets it against the docker-compose Postgres.
func pgTestURL(t *testing.T) (string, bool) {
	t.Helper()
	u := os.Getenv("ROLLOUT_TEST_DATABASE_URL")
	if u == "" {
		t.Skip("ROLLOUT_TEST_DATABASE_URL not set; skipping PostgreSQL integration test")
	}
	return u, true
}

// freshPG opens a connection, migrates, and truncates the mutable tables so the
// test is repeatable.
func freshPG(t *testing.T) *PG {
	t.Helper()
	url, _ := pgTestURL(t)
	ctx := context.Background()
	pg, err := NewPG(ctx, url)
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	t.Cleanup(pg.Close)
	if err := pg.Migrate(ctx); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	for _, tbl := range []string{"release_events", "observations", "releases", "used_nonces", "idempotency_keys", "threshold_versions"} {
		if _, err := pg.pool.Exec(ctx, "TRUNCATE TABLE "+tbl+" RESTART IDENTITY CASCADE"); err != nil {
			t.Fatalf("truncate %s: %v", tbl, err)
		}
	}
	// Re-seed the v1 policy directly and reset the sequence so the next
	// CreateThreshold produces version 2.
	spec, _ := json.Marshal(models.DefaultThresholdSpec())
	if _, err := pg.pool.Exec(ctx,
		`INSERT INTO threshold_versions(version, spec, description, created_at)
		 VALUES (1, $1::jsonb, 'initial default policy', now())`, string(spec)); err != nil {
		t.Fatalf("seed: %v", err)
	}
	if _, err := pg.pool.Exec(ctx, `SELECT setval(pg_get_serial_sequence('threshold_versions','version'), 1, true)`); err != nil {
		t.Fatalf("setval: %v", err)
	}
	return pg
}

func TestPGThresholdLifecycle(t *testing.T) {
	pg := freshPG(t)
	ctx := context.Background()

	tv, err := pg.GetLatestThreshold(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if tv.Version != 1 || tv.Spec.ErrorRateUpper != 0.05 {
		t.Fatalf("seeded policy wrong: %+v", tv)
	}
	newTV, err := pg.CreateThreshold(ctx, models.ThresholdSpec{
		ErrorRateUpper: 0.02, LatencyMeanMS: 100, LatencyP95MS: 180,
	}, "stricter")
	if err != nil {
		t.Fatal(err)
	}
	if newTV.Version != 2 {
		t.Fatalf("new version=%d want 2", newTV.Version)
	}
	got, _ := pg.GetThreshold(ctx, 1)
	if got.Spec.LatencyMeanMS != 120 {
		t.Fatalf("v1 must remain readable: %+v", got)
	}
}

func TestPGReleaseAndEvents(t *testing.T) {
	pg := freshPG(t)
	ctx := context.Background()

	now := time.Now().UTC().Truncate(time.Millisecond)
	r := models.Release{
		ID: "rel_pg_1", Name: "svc", Version: "v", State: models.StateActive,
		Stage: models.Stage5, StageWeight: 0.05, Generation: 0,
		ObservationMS: 400, MinSamples: 80, ThresholdVersion: 1,
		ThresholdSpec: models.DefaultThresholdSpec(), MetricURL: "http://x/metrics",
		Scenario: "healthy", StageEnteredAt: now, CreatedAt: now, UpdatedAt: now,
	}
	if err := pg.CreateRelease(ctx, r); err != nil {
		t.Fatal(err)
	}
	got, err := pg.GetRelease(ctx, r.ID)
	if err != nil {
		t.Fatal(err)
	}
	if got.ThresholdSpec.ErrorRateUpper != 0.05 || got.Stage != models.Stage5 {
		t.Fatalf("round-trip mismatch: %+v", got)
	}

	verdict := models.Verdict{Health: "healthy", ThresholdV: 1, Reasons: []models.Reason{models.ReasonOK}}
	err = pg.WithTx(ctx, func(tx Tx) error {
		if err := tx.InsertEvent(ctx, models.Event{
			ReleaseID: r.ID, Generation: 1, Type: "advanced",
			FromStage: models.Stage5, ToStage: models.Stage20,
			Verdict: &verdict, CreatedAt: now,
		}); err != nil {
			return err
		}
		return tx.InsertObservation(ctx, Observation{
			ReleaseID: r.ID, Stage: models.Stage5, Generation: 0,
			WindowStart: now.Format(time.RFC3339Nano),
			WindowEnd:   now.Add(400 * time.Millisecond).Format(time.RFC3339Nano),
			Verdict:     verdict,
		})
	})
	if err != nil {
		t.Fatal(err)
	}

	events, err := pg.ListEvents(ctx, r.ID)
	if err != nil || len(events) != 1 || events[0].Verdict == nil || events[0].Verdict.Health != "healthy" {
		t.Fatalf("events round-trip failed: %v events=%+v", err, events)
	}
	obs, err := pg.ListObservations(ctx, r.ID)
	if err != nil || len(obs) != 1 || obs[0].Verdict.ThresholdV != 1 {
		t.Fatalf("observations round-trip failed: %v obs=%+v", err, obs)
	}

	// Duplicate id must conflict.
	if err := pg.CreateRelease(ctx, r); !isUniqueViolation(err) && err == nil {
		t.Fatalf("expected unique violation on duplicate id, got %v", err)
	}
}

func TestPGGenerationOptimisticLock(t *testing.T) {
	pg := freshPG(t)
	ctx := context.Background()
	now := time.Now().UTC().Truncate(time.Millisecond)
	r := models.Release{
		ID: "rel_pg_lock", Name: "svc", Version: "v", State: models.StateActive,
		Stage: models.Stage5, StageWeight: 0.05, ObservationMS: 400, MinSamples: 80,
		ThresholdVersion: 1, ThresholdSpec: models.DefaultThresholdSpec(),
		MetricURL: "http://x/metrics", Scenario: "healthy",
		StageEnteredAt: now, CreatedAt: now, UpdatedAt: now,
	}
	if err := pg.CreateRelease(ctx, r); err != nil {
		t.Fatal(err)
	}

	// Two sequential transactions both lock the row FOR UPDATE and bump the
	// generation; each must see the committed value.
	for i := int64(1); i <= 3; i++ {
		err := pg.WithTx(ctx, func(tx Tx) error {
			cur, err := tx.GetReleaseForUpdate(ctx, r.ID)
			if err != nil {
				return err
			}
			cur.Generation = i
			cur.UpdatedAt = now
			return tx.UpdateRelease(ctx, cur)
		})
		if err != nil {
			t.Fatalf("tx %d: %v", i, err)
		}
	}
	got, _ := pg.GetRelease(ctx, r.ID)
	if got.Generation != 3 {
		t.Fatalf("generation=%d want 3", got.Generation)
	}
}

func TestPGNonceSingleUse(t *testing.T) {
	pg := freshPG(t)
	ctx := context.Background()
	exp := time.Now().Add(time.Minute).Unix()
	ok, err := pg.ConsumeNonce(ctx, "digest-a", exp)
	if err != nil || !ok {
		t.Fatalf("first consume: ok=%v err=%v", ok, err)
	}
	ok, err = pg.ConsumeNonce(ctx, "digest-a", exp)
	if err != nil || ok {
		t.Fatalf("second consume must be rejected: ok=%v err=%v", ok, err)
	}
	// A different nonce is fine.
	ok, _ = pg.ConsumeNonce(ctx, "digest-b", exp)
	if !ok {
		t.Fatal("different nonce must be accepted")
	}
}

func TestPGIdempotencyCache(t *testing.T) {
	pg := freshPG(t)
	ctx := context.Background()
	resp := StoredResponse{Status: 201, Body: []byte(`{"id":"rel_x"}`)}
	if err := pg.PutIdempotentResponse(ctx, "k1", resp); err != nil {
		t.Fatal(err)
	}
	got, ok, err := pg.GetIdempotentResponse(ctx, "k1")
	if err != nil || !ok || got.Status != 201 || string(got.Body) != `{"id":"rel_x"}` {
		t.Fatalf("idempotency round-trip: %v ok=%v got=%v", err, ok, string(got.Body))
	}
	// Verdict JSON serialization sanity (jsonb column path).
	b, _ := json.Marshal(models.Verdict{Health: "unknown"})
	if len(b) == 0 {
		t.Fatal("marshal verdict")
	}
}
