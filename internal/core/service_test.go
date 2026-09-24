package core_test

import (
	"context"
	"errors"
	"fmt"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/example/artifact-promotion/internal/blob"
	"github.com/example/artifact-promotion/internal/core"
	"github.com/example/artifact-promotion/internal/migrate"
	"github.com/jackc/pgx/v5/pgxpool"
)

var (
	testPool *pgxpool.Pool
	poolOnce sync.Once
	poolErr  error
)

func testDSN() string {
	if dsn := os.Getenv("TEST_DATABASE_URL"); dsn != "" {
		return dsn
	}
	return "postgres://promote:promote@localhost:5432/promotion_b_test?sslmode=disable"
}

func setup(t *testing.T, hooks core.Hooks) (*core.Service, *blob.Store) {
	t.Helper()
	poolOnce.Do(func() {
		ctx := context.Background()
		testPool, poolErr = pgxpool.New(ctx, testDSN())
		if poolErr != nil {
			return
		}
		poolErr = migrate.Apply(ctx, testPool)
	})
	if poolErr != nil {
		t.Skipf("test database unavailable: %v", poolErr)
	}
	ctx := context.Background()
	if _, err := testPool.Exec(ctx, `TRUNCATE environments, artifacts, evidence, policies, approvals, attempts, env_history`); err != nil {
		t.Fatalf("truncate: %v", err)
	}
	blobs, err := blob.New(t.TempDir())
	if err != nil {
		t.Fatalf("blob store: %v", err)
	}
	svc := core.NewService(testPool, blobs, hooks)
	if _, err := svc.CreateEnvironment(ctx, "test", 5, 30); err != nil {
		t.Fatalf("create test env: %v", err)
	}
	if _, err := svc.CreateEnvironment(ctx, "staging", 5, 30); err != nil {
		t.Fatalf("create staging env: %v", err)
	}
	return svc, blobs
}

// ingest uploads content into env and moves its pointer, returning the digest.
func ingest(t *testing.T, svc *core.Service, env, content string, expectedGen int64) string {
	t.Helper()
	a, err := svc.Ingest(context.Background(), env, "application/octet-stream", strings.NewReader(content), expectedGen)
	if err != nil {
		t.Fatalf("ingest into %s: %v", env, err)
	}
	if a.Status != core.StatusSucceeded {
		t.Fatalf("ingest into %s failed: %+v", env, a)
	}
	return a.ArtifactDigest
}

// prepare wires evidence, policy and approval so digest can be promoted
// test→staging, returning (evidenceVersion, policyVersion, approvalID).
func prepare(t *testing.T, svc *core.Service, digest string) (int, int, string) {
	t.Helper()
	ctx := context.Background()
	ev, err := svc.AddEvidence(ctx, "ev-1", digest, "integration", true, map[string]any{"tests": 42})
	if err != nil {
		t.Fatalf("add evidence: %v", err)
	}
	pol, err := svc.AddPolicy(ctx, "pol-1", "test", "staging", "integration", 1, nil)
	if err != nil {
		t.Fatalf("add policy: %v", err)
	}
	ap, err := svc.AddApproval(ctx, "staging", digest, "release-manager")
	if err != nil {
		t.Fatalf("add approval: %v", err)
	}
	return ev.Version, pol.Version, ap.ID
}

func promoteReq(digest string, evV, polV int, approvalID string, expectedGen int64) core.PromoteRequest {
	return core.PromoteRequest{
		SourceEnv: "test", TargetEnv: "staging", Digest: digest,
		EvidenceID: "ev-1", EvidenceVersion: evV,
		PolicyID: "pol-1", PolicyVersion: polV,
		ApprovalID: approvalID, ExpectedGeneration: expectedGen,
	}
}

func mustEnv(t *testing.T, svc *core.Service, name string) *core.Environment {
	t.Helper()
	e, err := svc.GetEnvironment(context.Background(), name)
	if err != nil {
		t.Fatalf("get env %s: %v", name, err)
	}
	return e
}

// ---------------------------------------------------------------------------
// Happy path
// ---------------------------------------------------------------------------

func TestPromoteHappyPath(t *testing.T) {
	svc, _ := setup(t, core.Hooks{})
	ctx := context.Background()

	digest := ingest(t, svc, "test", "artifact-v1-bytes", 0)
	evV, polV, apID := prepare(t, svc, digest)

	a, err := svc.Promote(ctx, promoteReq(digest, evV, polV, apID, 0))
	if err != nil {
		t.Fatalf("promote: %v", err)
	}
	if a.Status != core.StatusSucceeded {
		t.Fatalf("expected success, got %s (%v)", a.Status, a.FailureReason)
	}

	staging := mustEnv(t, svc, "staging")
	if staging.CurrentDigest == nil || *staging.CurrentDigest != digest {
		t.Fatalf("staging pointer = %v, want %s", staging.CurrentDigest, digest)
	}
	if staging.Generation != 1 {
		t.Fatalf("staging generation = %d, want 1", staging.Generation)
	}

	// Steps evidence recorded.
	names := map[string]bool{}
	for _, s := range a.Steps {
		names[s.Name] = true
	}
	for _, want := range []string{"attempt_created", "policy_validated", "evidence_validated", "source_pointer_validated", "approval_validated", "copy_verified", "pointer_committed"} {
		if !names[want] {
			t.Errorf("missing evidence step %q in %+v", want, a.Steps)
		}
	}

	// History recorded.
	hist, err := svc.EnvHistory(ctx, "staging")
	if err != nil {
		t.Fatalf("history: %v", err)
	}
	if len(hist) != 1 || hist[0].Digest != digest || !hist[0].Current || !hist[0].Complete {
		t.Fatalf("unexpected history: %+v", hist)
	}
}

// ---------------------------------------------------------------------------
// Concurrent promotions: expected-generation guard
// ---------------------------------------------------------------------------

func TestPromoteConcurrentConflict(t *testing.T) {
	svc, _ := setup(t, core.Hooks{
		// Hold every promotion between validation+copy and the pointer
		// commit, so both concurrent requests reach the commit with a
		// generation-0 approval — forcing the loser to hit the optimistic
		// guard itself rather than the earlier late-approval check.
		AfterCopyBeforeCommit: func() error {
			time.Sleep(150 * time.Millisecond)
			return nil
		},
	})
	ctx := context.Background()

	digest := ingest(t, svc, "test", "artifact-v1", 0)
	evV, polV, ap1 := prepare(t, svc, digest)
	ap2, err := svc.AddApproval(ctx, "staging", digest, "second-approver")
	if err != nil {
		t.Fatalf("second approval: %v", err)
	}

	// Two promotions race with the same expected generation.
	type res struct {
		a   *core.Attempt
		err error
	}
	ch := make(chan res, 2)
	for _, ap := range []string{ap1, ap2.ID} {
		go func(apID string) {
			a, err := svc.Promote(ctx, promoteReq(digest, evV, polV, apID, 0))
			ch <- res{a, err}
		}(ap)
	}
	r1, r2 := <-ch, <-ch

	var succeeded, conflicted int
	for _, r := range []res{r1, r2} {
		if r.err != nil {
			t.Fatalf("promote returned infra error: %v", r.err)
		}
		switch {
		case r.a.Status == core.StatusSucceeded:
			succeeded++
		case r.a.Status == core.StatusFailed && r.a.FailureReason != nil && *r.a.FailureReason == "generation_conflict":
			conflicted++
		default:
			reason := "<nil>"
			if r.a.FailureReason != nil {
				reason = *r.a.FailureReason
			}
			t.Fatalf("unexpected attempt outcome: status=%s reason=%s", r.a.Status, reason)
		}
	}
	if succeeded != 1 || conflicted != 1 {
		t.Fatalf("want 1 success + 1 conflict, got %d/%d", succeeded, conflicted)
	}
	if g := mustEnv(t, svc, "staging").Generation; g != 1 {
		t.Fatalf("generation = %d, want exactly 1 (no lost update)", g)
	}

	// Both attempts' evidence is persisted.
	attempts, err := svc.ListAttempts(ctx, "promotion", "")
	if err != nil {
		t.Fatalf("list attempts: %v", err)
	}
	if len(attempts) != 2 {
		t.Fatalf("want 2 recorded attempts, got %d", len(attempts))
	}
}

// ---------------------------------------------------------------------------
// Copy failure: pointer must not move
// ---------------------------------------------------------------------------

func TestPromoteCopyFailure(t *testing.T) {
	svc, blobs := setup(t, core.Hooks{})
	ctx := context.Background()

	digest := ingest(t, svc, "test", "artifact-v1", 0)
	evV, polV, apID := prepare(t, svc, digest)

	// Destroy the source blob after ingest: the copy must fail.
	if err := blobs.Remove("test", digest); err != nil {
		t.Fatalf("remove blob: %v", err)
	}

	a, err := svc.Promote(ctx, promoteReq(digest, evV, polV, apID, 0))
	if err != nil {
		t.Fatalf("promote: %v", err)
	}
	if a.Status != core.StatusFailed || a.FailureReason == nil || *a.FailureReason != "copy_failed" {
		t.Fatalf("expected copy_failed, got %+v", a)
	}
	staging := mustEnv(t, svc, "staging")
	if staging.CurrentDigest != nil || staging.Generation != 0 {
		t.Fatalf("staging pointer moved despite copy failure: %+v", staging)
	}
	// The failed attempt's evidence is retrievable.
	got, err := svc.GetAttempt(ctx, a.ID)
	if err != nil || got.Status != core.StatusFailed {
		t.Fatalf("failed attempt evidence not persisted: %v %+v", err, got)
	}
}

// ---------------------------------------------------------------------------
// Crash before pointer commit: old version stays live, attempt interrupted
// ---------------------------------------------------------------------------

func TestPromoteCrashBeforeCommit(t *testing.T) {
	crash := errors.New("simulated crash before pointer commit")
	svc, _ := setup(t, core.Hooks{AfterCopyBeforeCommit: func() error { return crash }})
	ctx := context.Background()

	digest := ingest(t, svc, "test", "artifact-v1", 0)
	evV, polV, apID := prepare(t, svc, digest)

	_, err := svc.Promote(ctx, promoteReq(digest, evV, polV, apID, 0))
	if !errors.Is(err, crash) {
		t.Fatalf("expected crash error, got %v", err)
	}

	// Pointer untouched: old (empty) version still live.
	staging := mustEnv(t, svc, "staging")
	if staging.CurrentDigest != nil || staging.Generation != 0 {
		t.Fatalf("pointer moved despite crash: %+v", staging)
	}

	// Attempt left running with the copy_verified step as evidence.
	attempts, err := svc.ListAttempts(ctx, "promotion", "running")
	if err != nil || len(attempts) != 1 {
		t.Fatalf("want 1 running attempt, got %v %+v", err, attempts)
	}
	var sawCopy bool
	for _, s := range attempts[0].Steps {
		if s.Name == "copy_verified" {
			sawCopy = true
		}
	}
	if !sawCopy {
		t.Fatalf("copy_verified step missing from evidence: %+v", attempts[0].Steps)
	}

	// Reconciler (runs at startup) marks it interrupted; pointer still intact.
	n, err := svc.ReconcileInterrupted(ctx, 0)
	if err != nil || n != 1 {
		t.Fatalf("reconcile: n=%d err=%v", n, err)
	}
	a, err := svc.GetAttempt(ctx, attempts[0].ID)
	if err != nil || a.Status != core.StatusInterrupted {
		t.Fatalf("attempt not interrupted: %v %+v", err, a)
	}
	if g := mustEnv(t, svc, "staging").Generation; g != 0 {
		t.Fatalf("generation = %d after reconcile, want 0", g)
	}
}

// ---------------------------------------------------------------------------
// Late approval
// ---------------------------------------------------------------------------

func TestPromoteLateApproval(t *testing.T) {
	svc, _ := setup(t, core.Hooks{})
	ctx := context.Background()

	digestA := ingest(t, svc, "test", "artifact-A", 0)
	evV, polV, apA := prepare(t, svc, digestA) // approval bound to staging gen 0

	// Promote A: staging moves to generation 1.
	a, err := svc.Promote(ctx, promoteReq(digestA, evV, polV, apA, 0))
	if err != nil || a.Status != core.StatusSucceeded {
		t.Fatalf("promote A: %v %+v", err, a)
	}

	// Promote B with a fresh approval: staging moves to generation 2.
	digestB := ingest(t, svc, "test", "artifact-B", 1)
	if _, err := svc.AddEvidence(ctx, "ev-1", digestB, "integration", true, nil); err != nil {
		t.Fatalf("evidence B: %v", err)
	}
	apB, err := svc.AddApproval(ctx, "staging", digestB, "release-manager")
	if err != nil {
		t.Fatalf("approval B: %v", err)
	}
	b, err := svc.Promote(ctx, promoteReq(digestB, 2, polV, apB.ID, 1))
	if err != nil || b.Status != core.StatusSucceeded {
		t.Fatalf("promote B: %v %+v", err, b)
	}

	// A stale approval for A (issued at staging gen 0) now arrives late.
	// Re-point test at A so only the approval check can fail.
	ingest(t, svc, "test", "artifact-A", 2)
	late, err := svc.Promote(ctx, promoteReq(digestA, evV, polV, apA, 2))
	if err != nil {
		t.Fatalf("late promote: %v", err)
	}
	// apA was consumed by the first promotion; either rejection proves the
	// stale approval cannot authorize a new promotion. Use a never-consumed
	// stale approval below for the specific late_approval check.
	if late.Status != core.StatusFailed {
		t.Fatalf("stale approval promoted! %+v", late)
	}

	apStale, err := svc.AddApproval(ctx, "staging", digestA, "release-manager")
	if err != nil {
		t.Fatalf("stale approval: %v", err)
	}
	// Move staging forward again so apStale (gen 2) becomes late.
	digestC := ingest(t, svc, "test", "artifact-C", 3)
	if _, err := svc.AddEvidence(ctx, "ev-1", digestC, "integration", true, nil); err != nil {
		t.Fatalf("evidence C: %v", err)
	}
	apC, err := svc.AddApproval(ctx, "staging", digestC, "release-manager")
	if err != nil {
		t.Fatalf("approval C: %v", err)
	}
	c, err := svc.Promote(ctx, promoteReq(digestC, 3, polV, apC.ID, 2))
	if err != nil || c.Status != core.StatusSucceeded {
		t.Fatalf("promote C: %v %+v", err, c)
	}

	ingest(t, svc, "test", "artifact-A", 4)
	late2, err := svc.Promote(ctx, promoteReq(digestA, evV, polV, apStale.ID, 3))
	if err != nil {
		t.Fatalf("late promote 2: %v", err)
	}
	if late2.Status != core.StatusFailed || late2.FailureReason == nil || *late2.FailureReason != "late_approval" {
		t.Fatalf("expected late_approval, got %+v", late2)
	}
	if g := mustEnv(t, svc, "staging").Generation; g != 3 {
		t.Fatalf("staging generation = %d, want 3 (late approval changed nothing)", g)
	}
}

// ---------------------------------------------------------------------------
// Rollback
// ---------------------------------------------------------------------------

// setupThreeVersions ingests A, B, C into test and promotes each to staging.
// Returns digests and the policy version. Staging ends at generation 3 on C.
func setupThreeVersions(t *testing.T, svc *core.Service) (dA, dB, dC string, polV int) {
	t.Helper()
	ctx := context.Background()
	pol, err := svc.AddPolicy(ctx, "pol-1", "test", "staging", "integration", 1, nil)
	if err != nil {
		t.Fatalf("policy: %v", err)
	}
	polV = pol.Version
	contents := []string{"artifact-A", "artifact-B", "artifact-C"}
	digests := make([]string, 3)
	for i, c := range contents {
		digests[i] = ingest(t, svc, "test", c, int64(i))
		if _, err := svc.AddEvidence(ctx, "ev-1", digests[i], "integration", true, nil); err != nil {
			t.Fatalf("evidence %d: %v", i, err)
		}
		ap, err := svc.AddApproval(ctx, "staging", digests[i], "release-manager")
		if err != nil {
			t.Fatalf("approval %d: %v", i, err)
		}
		a, err := svc.Promote(ctx, promoteReq(digests[i], i+1, polV, ap.ID, int64(i)))
		if err != nil || a.Status != core.StatusSucceeded {
			t.Fatalf("promote %d: %v %+v", i, err, a)
		}
	}
	return digests[0], digests[1], digests[2], polV
}

func TestRollbackHappyPath(t *testing.T) {
	svc, _ := setup(t, core.Hooks{})
	ctx := context.Background()
	dA, _, dC, _ := setupThreeVersions(t, svc)

	a, err := svc.Rollback(ctx, core.RollbackRequest{Environment: "staging", Digest: dA, ExpectedGeneration: 3})
	if err != nil {
		t.Fatalf("rollback: %v", err)
	}
	if a.Status != core.StatusSucceeded {
		t.Fatalf("rollback failed: %+v", a)
	}
	staging := mustEnv(t, svc, "staging")
	if staging.CurrentDigest == nil || *staging.CurrentDigest != dA || staging.Generation != 4 {
		t.Fatalf("staging = %+v, want digest A at generation 4", staging)
	}
	_ = dC
}

func TestRollbackRetentionExceeded(t *testing.T) {
	svc, _ := setup(t, core.Hooks{})
	ctx := context.Background()
	// Tighten retention: keep only the last 2 pointer values.
	if _, err := testPool.Exec(ctx, `UPDATE environments SET retention_keep = 2 WHERE name = 'staging'`); err != nil {
		t.Fatalf("set retention: %v", err)
	}
	dA, dB, _, _ := setupThreeVersions(t, svc)

	// A is 3 pointer-values back: rejected by retention.
	a, err := svc.Rollback(ctx, core.RollbackRequest{Environment: "staging", Digest: dA, ExpectedGeneration: 3})
	if err != nil {
		t.Fatalf("rollback A: %v", err)
	}
	if a.Status != core.StatusFailed || a.FailureReason == nil || *a.FailureReason != "retention_exceeded" {
		t.Fatalf("expected retention_exceeded for A, got %+v", a)
	}
	// B is 2 back: allowed.
	b, err := svc.Rollback(ctx, core.RollbackRequest{Environment: "staging", Digest: dB, ExpectedGeneration: 3})
	if err != nil {
		t.Fatalf("rollback B: %v", err)
	}
	if b.Status != core.StatusSucceeded {
		t.Fatalf("rollback to B should succeed, got %+v", b)
	}
}

func TestRollbackIncompleteArtifact(t *testing.T) {
	svc, blobs := setup(t, core.Hooks{})
	ctx := context.Background()
	dA, _, _, _ := setupThreeVersions(t, svc)

	// Corrupt A's blob in staging: delete it (or it could be truncated).
	if err := blobs.Remove("staging", dA); err != nil {
		t.Fatalf("remove blob: %v", err)
	}
	a, err := svc.Rollback(ctx, core.RollbackRequest{Environment: "staging", Digest: dA, ExpectedGeneration: 3})
	if err != nil {
		t.Fatalf("rollback: %v", err)
	}
	if a.Status != core.StatusFailed || a.FailureReason == nil || *a.FailureReason != "blob_incomplete" {
		t.Fatalf("expected blob_incomplete, got %+v", a)
	}
	if g := mustEnv(t, svc, "staging").Generation; g != 3 {
		t.Fatalf("generation moved despite incomplete blob: %d", g)
	}
}

func TestRollbackNotInHistory(t *testing.T) {
	svc, _ := setup(t, core.Hooks{})
	ctx := context.Background()
	_, _, _, _ = setupThreeVersions(t, svc)

	ghost := ingest(t, svc, "test", "never-promoted", 3)
	a, err := svc.Rollback(ctx, core.RollbackRequest{Environment: "staging", Digest: ghost, ExpectedGeneration: 3})
	if err != nil {
		t.Fatalf("rollback: %v", err)
	}
	if a.Status != core.StatusFailed || a.FailureReason == nil || *a.FailureReason != "not_in_history" {
		t.Fatalf("expected not_in_history, got %+v", a)
	}
}

func TestRollbackConcurrent(t *testing.T) {
	svc, _ := setup(t, core.Hooks{})
	ctx := context.Background()
	dA, dB, _, _ := setupThreeVersions(t, svc)

	type res struct {
		a   *core.Attempt
		err error
	}
	ch := make(chan res, 2)
	for _, d := range []string{dA, dB} {
		go func(digest string) {
			a, err := svc.Rollback(ctx, core.RollbackRequest{Environment: "staging", Digest: digest, ExpectedGeneration: 3})
			ch <- res{a, err}
		}(d)
	}
	r1, r2 := <-ch, <-ch
	var succeeded, conflicted int
	for _, r := range []res{r1, r2} {
		if r.err != nil {
			t.Fatalf("rollback infra error: %v", r.err)
		}
		switch {
		case r.a.Status == core.StatusSucceeded:
			succeeded++
		case r.a.Status == core.StatusFailed && r.a.FailureReason != nil && *r.a.FailureReason == "generation_conflict":
			conflicted++
		default:
			t.Fatalf("unexpected outcome: %+v", r.a)
		}
	}
	if succeeded != 1 || conflicted != 1 {
		t.Fatalf("want 1 success + 1 conflict, got %d/%d", succeeded, conflicted)
	}
	if g := mustEnv(t, svc, "staging").Generation; g != 4 {
		t.Fatalf("generation = %d, want 4", g)
	}
}

// ---------------------------------------------------------------------------
// Policy / evidence enforcement
// ---------------------------------------------------------------------------

func TestPromoteEvidenceFailures(t *testing.T) {
	svc, _ := setup(t, core.Hooks{})
	ctx := context.Background()

	digest := ingest(t, svc, "test", "artifact-v1", 0)
	pol, err := svc.AddPolicy(ctx, "pol-1", "test", "staging", "integration", 2, nil)
	if err != nil {
		t.Fatalf("policy: %v", err)
	}
	// v1: failed suite. v2: passed but below policy minimum? No — v2 meets min.
	// Use v1 (failed) and check both rejection classes.
	evFail, err := svc.AddEvidence(ctx, "ev-1", digest, "integration", false, nil)
	if err != nil {
		t.Fatalf("evidence fail: %v", err)
	}
	evPass, err := svc.AddEvidence(ctx, "ev-1", digest, "integration", true, nil)
	if err != nil {
		t.Fatalf("evidence pass: %v", err)
	}
	ap, err := svc.AddApproval(ctx, "staging", digest, "release-manager")
	if err != nil {
		t.Fatalf("approval: %v", err)
	}

	// Failed evidence.
	a, err := svc.Promote(ctx, promoteReq(digest, evFail.Version, pol.Version, ap.ID, 0))
	if err != nil {
		t.Fatalf("promote: %v", err)
	}
	if a.Status != core.StatusFailed || *a.FailureReason != "evidence_not_passed" {
		t.Fatalf("expected evidence_not_passed, got %+v", a)
	}

	// Passing evidence but below the policy's minimum version (min=2, and
	// evPass IS v2, so craft a lower one): policy requires >= 2, submit v1's
	// number with a passing flag — v1 is failed, so instead lower min check
	// with a fresh policy at min 3.
	pol3, err := svc.AddPolicy(ctx, "pol-1", "test", "staging", "integration", 3, nil)
	if err != nil {
		t.Fatalf("policy v2: %v", err)
	}
	b, err := svc.Promote(ctx, promoteReq(digest, evPass.Version, pol3.Version, ap.ID, 0))
	if err != nil {
		t.Fatalf("promote: %v", err)
	}
	if b.Status != core.StatusFailed || *b.FailureReason != "evidence_version_too_low" {
		t.Fatalf("expected evidence_version_too_low, got %+v", b)
	}
	if g := mustEnv(t, svc, "staging").Generation; g != 0 {
		t.Fatalf("generation moved despite rejections: %d", g)
	}
}

func TestApprovalSingleUse(t *testing.T) {
	svc, _ := setup(t, core.Hooks{})
	ctx := context.Background()

	digest := ingest(t, svc, "test", "artifact-v1", 0)
	evV, polV, apID := prepare(t, svc, digest)

	a, err := svc.Promote(ctx, promoteReq(digest, evV, polV, apID, 0))
	if err != nil || a.Status != core.StatusSucceeded {
		t.Fatalf("first promote: %v %+v", err, a)
	}
	// Roll staging back, then try to re-promote with the SAME approval.
	dOther := ingest(t, svc, "test", "artifact-v2", 1)
	if _, err := svc.AddEvidence(ctx, "ev-1", dOther, "integration", true, nil); err != nil {
		t.Fatalf("evidence v2: %v", err)
	}
	ap2, err := svc.AddApproval(ctx, "staging", dOther, "release-manager")
	if err != nil {
		t.Fatalf("approval v2: %v", err)
	}
	if _, err := svc.Promote(ctx, promoteReq(dOther, 2, polV, ap2.ID, 1)); err != nil {
		t.Fatalf("promote v2: %v", err)
	}
	if _, err := svc.Rollback(ctx, core.RollbackRequest{Environment: "staging", Digest: digest, ExpectedGeneration: 2}); err != nil {
		t.Fatalf("rollback: %v", err)
	}
	ingest(t, svc, "test", "artifact-v1", 2)
	replay, err := svc.Promote(ctx, promoteReq(digest, evV, polV, apID, 3))
	if err != nil {
		t.Fatalf("replay promote: %v", err)
	}
	if replay.Status != core.StatusFailed || *replay.FailureReason != "approval_consumed" {
		t.Fatalf("expected approval_consumed, got %+v", replay)
	}
}

// svcDB exposes the pool for test-only direct assertions.
var _ = fmt.Sprintf
var _ = time.Now
