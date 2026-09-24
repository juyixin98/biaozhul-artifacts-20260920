package core

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"time"

	"github.com/example/artifact-promotion/internal/blob"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

var (
	// ErrNotFound is returned for missing environments, attempts, etc.
	ErrNotFound = errors.New("not found")
	// ErrInvalid is returned for malformed requests (bad digest, bad env pair).
	ErrInvalid = errors.New("invalid request")

	errGenerationConflict = errors.New("generation conflict")
	errApprovalConsumed   = errors.New("approval already consumed")
)

// Hooks allows tests to inject failures at exact points in the commit path.
type Hooks struct {
	// AfterCopyBeforeCommit runs after the blob copy has been verified and
	// before the pointer-commit transaction begins. Returning an error
	// simulates a crash at the worst possible moment: the copy is done, the
	// pointer has not moved, and the attempt is left 'running'.
	AfterCopyBeforeCommit func() error
}

type Service struct {
	db    *pgxpool.Pool
	blobs *blob.Store
	hooks Hooks
	now   func() time.Time
}

func NewService(db *pgxpool.Pool, blobs *blob.Store, hooks Hooks) *Service {
	return &Service{db: db, blobs: blobs, hooks: hooks, now: func() time.Time { return time.Now().UTC() }}
}

// ---------------------------------------------------------------------------
// Environments
// ---------------------------------------------------------------------------

func (s *Service) CreateEnvironment(ctx context.Context, name string, retentionKeep, retentionMaxAgeDays int) (*Environment, error) {
	if name == "" {
		return nil, fmt.Errorf("%w: environment name required", ErrInvalid)
	}
	if retentionKeep <= 0 {
		retentionKeep = 5
	}
	if retentionMaxAgeDays <= 0 {
		retentionMaxAgeDays = 30
	}
	_, err := s.db.Exec(ctx,
		`INSERT INTO environments (name, retention_keep, retention_max_age_days)
		 VALUES ($1, $2, $3)
		 ON CONFLICT (name) DO NOTHING`, name, retentionKeep, retentionMaxAgeDays)
	if err != nil {
		return nil, fmt.Errorf("create environment: %w", err)
	}
	return s.GetEnvironment(ctx, name)
}

func (s *Service) GetEnvironment(ctx context.Context, name string) (*Environment, error) {
	var e Environment
	err := s.db.QueryRow(ctx,
		`SELECT name, current_digest, generation, retention_keep, retention_max_age_days, created_at, updated_at
		 FROM environments WHERE name = $1`, name).
		Scan(&e.Name, &e.CurrentDigest, &e.Generation, &e.RetentionKeep, &e.RetentionMaxAgeDays, &e.CreatedAt, &e.UpdatedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, fmt.Errorf("environment %q: %w", name, ErrNotFound)
	}
	if err != nil {
		return nil, err
	}
	return &e, nil
}

func (s *Service) ListEnvironments(ctx context.Context) ([]Environment, error) {
	rows, err := s.db.Query(ctx,
		`SELECT name, current_digest, generation, retention_keep, retention_max_age_days, created_at, updated_at
		 FROM environments ORDER BY name`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []Environment{}
	for rows.Next() {
		var e Environment
		if err := rows.Scan(&e.Name, &e.CurrentDigest, &e.Generation, &e.RetentionKeep, &e.RetentionMaxAgeDays, &e.CreatedAt, &e.UpdatedAt); err != nil {
			return nil, err
		}
		out = append(out, e)
	}
	return out, rows.Err()
}

// ---------------------------------------------------------------------------
// Ingest: land a freshly built artifact in an environment
// ---------------------------------------------------------------------------

// Ingest stores the uploaded bytes in env's blob namespace (digest computed
// from the actual bytes) and atomically advances env's pointer to the new
// digest, guarded by expectedGeneration. The ingest itself is recorded as an
// attempt, so every pointer movement in the system has evidence.
func (s *Service) Ingest(ctx context.Context, env string, mediaType string, r io.Reader, expectedGeneration int64) (*Attempt, error) {
	if _, err := s.GetEnvironment(ctx, env); err != nil {
		return nil, err
	}
	if mediaType == "" {
		mediaType = "application/octet-stream"
	}
	digest, size, err := s.blobs.Put(env, r)
	if err != nil {
		return nil, fmt.Errorf("store blob: %w", err)
	}
	if _, err := s.db.Exec(ctx,
		`INSERT INTO artifacts (digest, size_bytes, media_type) VALUES ($1, $2, $3)
		 ON CONFLICT (digest) DO NOTHING`, digest, size, mediaType); err != nil {
		return nil, fmt.Errorf("register artifact: %w", err)
	}

	id := uuid.New()
	if err := s.insertAttempt(ctx, id, KindIngest, env, env, digest, expectedGeneration, attemptRefs{}); err != nil {
		return nil, err
	}
	s.addStep(ctx, id, "blob_stored", fmt.Sprintf("bytes=%d digest=%s", size, digest))

	if err := s.commitPointer(ctx, id, env, digest, expectedGeneration, nil); err != nil {
		if errors.Is(err, errGenerationConflict) {
			return s.reject(ctx, id, "generation_conflict",
				fmt.Sprintf("environment %q is no longer at generation %d", env, expectedGeneration))
		}
		return nil, err
	}
	return s.GetAttempt(ctx, id.String())
}

// ---------------------------------------------------------------------------
// Evidence, policies, approvals
// ---------------------------------------------------------------------------

func (s *Service) AddEvidence(ctx context.Context, id, digest, suite string, passed bool, report map[string]any) (*Evidence, error) {
	if id == "" || suite == "" {
		return nil, fmt.Errorf("%w: evidence id and suite required", ErrInvalid)
	}
	if err := blob.ValidateDigest(digest); err != nil {
		return nil, fmt.Errorf("%w: %v", ErrInvalid, err)
	}
	rep, err := json.Marshal(report)
	if err != nil {
		return nil, fmt.Errorf("%w: report must be JSON object", ErrInvalid)
	}
	var e Evidence
	var rawReport []byte
	err = s.db.QueryRow(ctx,
		`INSERT INTO evidence (id, version, artifact_digest, suite, passed, report)
		 VALUES ($1, (SELECT COALESCE(MAX(version), 0) + 1 FROM evidence WHERE id = $1), $2, $3, $4, $5)
		 RETURNING id, version, artifact_digest, suite, passed, report, created_at`,
		id, digest, suite, passed, string(rep)).
		Scan(&e.ID, &e.Version, &e.ArtifactDigest, &e.Suite, &e.Passed, &rawReport, &e.CreatedAt)
	if err != nil {
		return nil, fmt.Errorf("insert evidence: %w", err)
	}
	_ = json.Unmarshal(rawReport, &e.Report)
	return &e, nil
}

func (s *Service) AddPolicy(ctx context.Context, id, sourceEnv, targetEnv, requiredSuite string, minEvidenceVersion int, body map[string]any) (*Policy, error) {
	if id == "" || sourceEnv == "" || targetEnv == "" || requiredSuite == "" {
		return nil, fmt.Errorf("%w: policy id, source_env, target_env, required_suite required", ErrInvalid)
	}
	if minEvidenceVersion <= 0 {
		minEvidenceVersion = 1
	}
	b, err := json.Marshal(body)
	if err != nil {
		return nil, fmt.Errorf("%w: body must be JSON object", ErrInvalid)
	}
	var p Policy
	var rawBody []byte
	err = s.db.QueryRow(ctx,
		`INSERT INTO policies (id, version, source_env, target_env, required_suite, min_evidence_version, body)
		 VALUES ($1, (SELECT COALESCE(MAX(version), 0) + 1 FROM policies WHERE id = $1), $2, $3, $4, $5, $6)
		 RETURNING id, version, source_env, target_env, required_suite, min_evidence_version, body, created_at`,
		id, sourceEnv, targetEnv, requiredSuite, minEvidenceVersion, string(b)).
		Scan(&p.ID, &p.Version, &p.SourceEnv, &p.TargetEnv, &p.RequiredSuite, &p.MinEvidenceVersion, &rawBody, &p.CreatedAt)
	if err != nil {
		return nil, fmt.Errorf("insert policy: %w", err)
	}
	_ = json.Unmarshal(rawBody, &p.Body)
	return &p, nil
}

// AddApproval issues an approval bound to the environment's *current*
// generation. If the environment advances before the approval is used, the
// approval is late and will be rejected.
func (s *Service) AddApproval(ctx context.Context, env, digest, approver string) (*Approval, error) {
	if err := blob.ValidateDigest(digest); err != nil {
		return nil, fmt.Errorf("%w: %v", ErrInvalid, err)
	}
	if approver == "" {
		return nil, fmt.Errorf("%w: approver required", ErrInvalid)
	}
	e, err := s.GetEnvironment(ctx, env)
	if err != nil {
		return nil, err
	}
	var a Approval
	err = s.db.QueryRow(ctx,
		`INSERT INTO approvals (id, environment, generation, artifact_digest, approver)
		 VALUES ($1, $2, $3, $4, $5)
		 RETURNING id, environment, generation, artifact_digest, approver, created_at, consumed_at`,
		uuid.New(), env, e.Generation, digest, approver).
		Scan(&a.ID, &a.Environment, &a.Generation, &a.ArtifactDigest, &a.Approver, &a.CreatedAt, &a.ConsumedAt)
	if err != nil {
		return nil, fmt.Errorf("insert approval: %w", err)
	}
	return &a, nil
}

// ---------------------------------------------------------------------------
// Promotion
// ---------------------------------------------------------------------------

// Promote copies digest from source to target environment and, only after
// the copy is verified, commits the target pointer in one transaction
// guarded by req.ExpectedGeneration. Domain rejections return a failed
// Attempt (evidence preserved) and a nil error; infrastructure failures
// return an error.
func (s *Service) Promote(ctx context.Context, req PromoteRequest) (*Attempt, error) {
	if req.SourceEnv == "" || req.TargetEnv == "" || req.SourceEnv == req.TargetEnv {
		return nil, fmt.Errorf("%w: source_env and target_env must differ and be non-empty", ErrInvalid)
	}
	if err := blob.ValidateDigest(req.Digest); err != nil {
		return nil, fmt.Errorf("%w: %v", ErrInvalid, err)
	}
	approvalID, err := uuid.Parse(req.ApprovalID)
	if err != nil {
		return nil, fmt.Errorf("%w: approval_id must be a UUID", ErrInvalid)
	}

	id := uuid.New()
	refs := attemptRefs{
		policyID: &req.PolicyID, policyVersion: &req.PolicyVersion,
		evidenceID: &req.EvidenceID, evidenceVersion: &req.EvidenceVersion,
		approvalID: &approvalID,
	}
	if err := s.insertAttempt(ctx, id, KindPromotion, req.SourceEnv, req.TargetEnv, req.Digest, req.ExpectedGeneration, refs); err != nil {
		return nil, err
	}

	// 1. Policy must exist and govern exactly this source→target pair.
	var p Policy
	err = s.db.QueryRow(ctx,
		`SELECT id, version, source_env, target_env, required_suite, min_evidence_version
		 FROM policies WHERE id = $1 AND version = $2`, req.PolicyID, req.PolicyVersion).
		Scan(&p.ID, &p.Version, &p.SourceEnv, &p.TargetEnv, &p.RequiredSuite, &p.MinEvidenceVersion)
	if errors.Is(err, pgx.ErrNoRows) {
		return s.reject(ctx, id, "policy_not_found", fmt.Sprintf("policy %s v%d does not exist", req.PolicyID, req.PolicyVersion))
	}
	if err != nil {
		return nil, err
	}
	if p.SourceEnv != req.SourceEnv || p.TargetEnv != req.TargetEnv {
		return s.reject(ctx, id, "policy_mismatch",
			fmt.Sprintf("policy %s v%d governs %s→%s, not %s→%s", p.ID, p.Version, p.SourceEnv, p.TargetEnv, req.SourceEnv, req.TargetEnv))
	}
	s.addStep(ctx, id, "policy_validated", fmt.Sprintf("policy=%s v%d", p.ID, p.Version))

	// 2. Evidence must exist, match the digest, have passed, and satisfy the
	//    policy's suite and minimum version.
	var ev Evidence
	err = s.db.QueryRow(ctx,
		`SELECT id, version, artifact_digest, suite, passed FROM evidence WHERE id = $1 AND version = $2`,
		req.EvidenceID, req.EvidenceVersion).
		Scan(&ev.ID, &ev.Version, &ev.ArtifactDigest, &ev.Suite, &ev.Passed)
	if errors.Is(err, pgx.ErrNoRows) {
		return s.reject(ctx, id, "evidence_not_found", fmt.Sprintf("evidence %s v%d does not exist", req.EvidenceID, req.EvidenceVersion))
	}
	if err != nil {
		return nil, err
	}
	if ev.ArtifactDigest != req.Digest {
		return s.reject(ctx, id, "evidence_digest_mismatch",
			fmt.Sprintf("evidence %s v%d covers %s, not %s", ev.ID, ev.Version, ev.ArtifactDigest, req.Digest))
	}
	if !ev.Passed {
		return s.reject(ctx, id, "evidence_not_passed", fmt.Sprintf("evidence %s v%d did not pass", ev.ID, ev.Version))
	}
	if ev.Suite != p.RequiredSuite {
		return s.reject(ctx, id, "evidence_suite_mismatch",
			fmt.Sprintf("policy requires suite %q, evidence ran %q", p.RequiredSuite, ev.Suite))
	}
	if ev.Version < p.MinEvidenceVersion {
		return s.reject(ctx, id, "evidence_version_too_low",
			fmt.Sprintf("policy requires evidence >= v%d, got v%d", p.MinEvidenceVersion, ev.Version))
	}
	s.addStep(ctx, id, "evidence_validated", fmt.Sprintf("evidence=%s v%d suite=%s", ev.ID, ev.Version, ev.Suite))

	// 3. The digest must be what the source environment currently runs.
	src, err := s.GetEnvironment(ctx, req.SourceEnv)
	if err != nil {
		if errors.Is(err, ErrNotFound) {
			return s.reject(ctx, id, "source_env_not_found", req.SourceEnv)
		}
		return nil, err
	}
	if src.CurrentDigest == nil || *src.CurrentDigest != req.Digest {
		cur := "<none>"
		if src.CurrentDigest != nil {
			cur = *src.CurrentDigest
		}
		return s.reject(ctx, id, "source_pointer_mismatch",
			fmt.Sprintf("source env %q currently runs %s, not %s", req.SourceEnv, cur, req.Digest))
	}
	s.addStep(ctx, id, "source_pointer_validated", fmt.Sprintf("source=%s generation=%d", src.Name, src.Generation))

	// 4. Approval must exist, match env+digest, be unconsumed, and not be late.
	var aEnv, aDigest string
	var aGen int64
	var aConsumed *time.Time
	err = s.db.QueryRow(ctx,
		`SELECT environment, generation, artifact_digest, consumed_at FROM approvals WHERE id = $1`, approvalID).
		Scan(&aEnv, &aGen, &aDigest, &aConsumed)
	if errors.Is(err, pgx.ErrNoRows) {
		return s.reject(ctx, id, "approval_not_found", req.ApprovalID)
	}
	if err != nil {
		return nil, err
	}
	if aEnv != req.TargetEnv {
		return s.reject(ctx, id, "approval_env_mismatch", fmt.Sprintf("approval is for env %q, not %q", aEnv, req.TargetEnv))
	}
	if aDigest != req.Digest {
		return s.reject(ctx, id, "approval_digest_mismatch", fmt.Sprintf("approval covers %s, not %s", aDigest, req.Digest))
	}
	if aConsumed != nil {
		return s.reject(ctx, id, "approval_consumed", fmt.Sprintf("approval consumed at %s", aConsumed.Format(time.RFC3339)))
	}
	tgt, err := s.GetEnvironment(ctx, req.TargetEnv)
	if err != nil {
		if errors.Is(err, ErrNotFound) {
			return s.reject(ctx, id, "target_env_not_found", req.TargetEnv)
		}
		return nil, err
	}
	if aGen < tgt.Generation {
		return s.reject(ctx, id, "late_approval",
			fmt.Sprintf("approval issued for generation %d, but env %q is now at generation %d", aGen, req.TargetEnv, tgt.Generation))
	}
	if aGen > tgt.Generation {
		return s.reject(ctx, id, "generation_conflict",
			fmt.Sprintf("approval references future generation %d (env at %d)", aGen, tgt.Generation))
	}
	s.addStep(ctx, id, "approval_validated", fmt.Sprintf("approval=%s generation=%d", approvalID, aGen))

	// 5. Copy the blob and verify the digest of the bytes actually written.
	size, err := s.blobs.Copy(req.SourceEnv, req.TargetEnv, req.Digest)
	if err != nil {
		return s.reject(ctx, id, "copy_failed", err.Error())
	}
	s.addStep(ctx, id, "copy_verified", fmt.Sprintf("bytes=%d digest=%s", size, req.Digest))

	// Fault-injection point: simulates a crash after the copy, before the
	// pointer commit. The attempt stays 'running' and the old pointer lives.
	if s.hooks.AfterCopyBeforeCommit != nil {
		if err := s.hooks.AfterCopyBeforeCommit(); err != nil {
			return nil, err
		}
	}

	// 6. Commit the pointer switch atomically with the evidence update.
	if err := s.commitPointer(ctx, id, req.TargetEnv, req.Digest, req.ExpectedGeneration, &approvalID); err != nil {
		switch {
		case errors.Is(err, errGenerationConflict):
			return s.reject(ctx, id, "generation_conflict",
				fmt.Sprintf("environment %q is no longer at generation %d; a concurrent attempt won", req.TargetEnv, req.ExpectedGeneration))
		case errors.Is(err, errApprovalConsumed):
			return s.reject(ctx, id, "approval_consumed", "approval was consumed by a concurrent promotion")
		default:
			return nil, err
		}
	}
	return s.GetAttempt(ctx, id.String())
}

// ---------------------------------------------------------------------------
// Rollback
// ---------------------------------------------------------------------------

// Rollback points env back at a historical digest. The target must be in the
// environment's pointer history, still satisfy the retention rules, and its
// blob must pass a fresh sha256 integrity check. The pointer commit uses the
// same optimistic-concurrency guard as promotion.
func (s *Service) Rollback(ctx context.Context, req RollbackRequest) (*Attempt, error) {
	if req.Environment == "" {
		return nil, fmt.Errorf("%w: environment required", ErrInvalid)
	}
	if err := blob.ValidateDigest(req.Digest); err != nil {
		return nil, fmt.Errorf("%w: %v", ErrInvalid, err)
	}

	id := uuid.New()
	if err := s.insertAttempt(ctx, id, KindRollback, req.Environment, req.Environment, req.Digest, req.ExpectedGeneration, attemptRefs{}); err != nil {
		return nil, err
	}

	env, err := s.GetEnvironment(ctx, req.Environment)
	if err != nil {
		if errors.Is(err, ErrNotFound) {
			return s.reject(ctx, id, "environment_not_found", req.Environment)
		}
		return nil, err
	}
	if env.CurrentDigest != nil && *env.CurrentDigest == req.Digest {
		return s.reject(ctx, id, "already_current", fmt.Sprintf("env %q already runs %s", req.Environment, req.Digest))
	}

	// Must be in history, and its most recent appearance must satisfy the
	// retention rules (within the last retention_keep pointer values and not
	// older than retention_max_age_days).
	var rank int64
	var replacedAt time.Time
	err = s.db.QueryRow(ctx,
		`WITH ranked AS (
		   SELECT generation, digest, replaced_at,
		          ROW_NUMBER() OVER (ORDER BY generation DESC) AS rank
		   FROM env_history WHERE environment = $1
		 )
		 SELECT rank, replaced_at FROM ranked WHERE digest = $2
		 ORDER BY generation DESC LIMIT 1`, req.Environment, req.Digest).
		Scan(&rank, &replacedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return s.reject(ctx, id, "not_in_history", fmt.Sprintf("digest %s never ran in env %q", req.Digest, req.Environment))
	}
	if err != nil {
		return nil, err
	}
	if rank > int64(env.RetentionKeep) {
		return s.reject(ctx, id, "retention_exceeded",
			fmt.Sprintf("digest is %d pointer-values back, retention keeps last %d", rank, env.RetentionKeep))
	}
	if age := s.now().Sub(replacedAt); age > time.Duration(env.RetentionMaxAgeDays)*24*time.Hour {
		return s.reject(ctx, id, "retention_exceeded",
			fmt.Sprintf("digest left service %s ago, retention allows %d days", age.Round(time.Minute), env.RetentionMaxAgeDays))
	}
	s.addStep(ctx, id, "retention_validated", fmt.Sprintf("rank=%d keep=%d", rank, env.RetentionKeep))

	// Completeness: recompute the blob's sha256 right now.
	size, err := s.blobs.Verify(req.Environment, req.Digest)
	if err != nil {
		return s.reject(ctx, id, "blob_incomplete", err.Error())
	}
	s.addStep(ctx, id, "integrity_verified", fmt.Sprintf("bytes=%d digest=%s", size, req.Digest))

	if s.hooks.AfterCopyBeforeCommit != nil {
		if err := s.hooks.AfterCopyBeforeCommit(); err != nil {
			return nil, err
		}
	}

	if err := s.commitPointer(ctx, id, req.Environment, req.Digest, req.ExpectedGeneration, nil); err != nil {
		if errors.Is(err, errGenerationConflict) {
			return s.reject(ctx, id, "generation_conflict",
				fmt.Sprintf("environment %q is no longer at generation %d; a concurrent attempt won", req.Environment, req.ExpectedGeneration))
		}
		return nil, err
	}
	return s.GetAttempt(ctx, id.String())
}

// ---------------------------------------------------------------------------
// Pointer commit — the single atomic switch
// ---------------------------------------------------------------------------

// commitPointer performs the atomic pointer switch in one transaction:
// optimistic generation guard, history append, approval consumption, and the
// attempt's success record. If the transaction commits, the new version is
// live; if anything fails, the old version remains live, untouched.
func (s *Service) commitPointer(ctx context.Context, attemptID uuid.UUID, env, digest string, expectedGeneration int64, approvalID *uuid.UUID) error {
	tx, err := s.db.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)

	tag, err := tx.Exec(ctx,
		`UPDATE environments
		 SET current_digest = $1, generation = generation + 1, updated_at = now()
		 WHERE name = $2 AND generation = $3`, digest, env, expectedGeneration)
	if err != nil {
		return fmt.Errorf("pointer update: %w", err)
	}
	if tag.RowsAffected() == 0 {
		return errGenerationConflict
	}
	newGen := expectedGeneration + 1
	if _, err := tx.Exec(ctx,
		`INSERT INTO env_history (environment, generation, digest, attempt_id) VALUES ($1, $2, $3, $4)`,
		env, newGen, digest, attemptID); err != nil {
		return fmt.Errorf("history insert: %w", err)
	}
	if approvalID != nil {
		tag, err := tx.Exec(ctx,
			`UPDATE approvals SET consumed_at = now() WHERE id = $1 AND consumed_at IS NULL`, *approvalID)
		if err != nil {
			return fmt.Errorf("consume approval: %w", err)
		}
		if tag.RowsAffected() == 0 {
			return errApprovalConsumed
		}
	}
	step, _ := json.Marshal([]Step{{Name: "pointer_committed", Detail: fmt.Sprintf("env=%s generation=%d digest=%s", env, newGen, digest), At: s.now()}})
	if _, err := tx.Exec(ctx,
		`UPDATE attempts SET status = $2, finished_at = now(), steps = steps || $3::jsonb WHERE id = $1`,
		attemptID, StatusSucceeded, string(step)); err != nil {
		return fmt.Errorf("attempt success record: %w", err)
	}
	return tx.Commit(ctx)
}

// ---------------------------------------------------------------------------
// Attempt evidence
// ---------------------------------------------------------------------------

type attemptRefs struct {
	policyID        *string
	policyVersion   *int
	evidenceID      *string
	evidenceVersion *int
	approvalID      *uuid.UUID
}

func (s *Service) insertAttempt(ctx context.Context, id uuid.UUID, kind, srcEnv, tgtEnv, digest string, expectedGen int64, refs attemptRefs) error {
	_, err := s.db.Exec(ctx,
		`INSERT INTO attempts
		   (id, kind, source_env, target_env, artifact_digest, expected_generation,
		    policy_id, policy_version, evidence_id, evidence_version, approval_id)
		 VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)`,
		id, kind, srcEnv, tgtEnv, digest, expectedGen,
		refs.policyID, refs.policyVersion, refs.evidenceID, refs.evidenceVersion, refs.approvalID)
	if err != nil {
		return fmt.Errorf("insert attempt: %w", err)
	}
	s.addStep(ctx, id, "attempt_created",
		fmt.Sprintf("kind=%s digest=%s expected_generation=%d", kind, digest, expectedGen))
	return nil
}

// addStep appends one evidence step immediately (its own statement), so the
// log survives a crash at any later point.
func (s *Service) addStep(ctx context.Context, attemptID uuid.UUID, name, detail string) {
	step, _ := json.Marshal([]Step{{Name: name, Detail: detail, At: s.now()}})
	_, _ = s.db.Exec(ctx,
		`UPDATE attempts SET steps = steps || $2::jsonb WHERE id = $1`, attemptID, string(step))
}

// reject records a domain rejection as a failed attempt and returns it.
func (s *Service) reject(ctx context.Context, id uuid.UUID, reason, detail string) (*Attempt, error) {
	s.addStep(ctx, id, "rejected", reason+": "+detail)
	if _, err := s.db.Exec(ctx,
		`UPDATE attempts SET status = $2, failure_reason = $3, finished_at = now() WHERE id = $1`,
		id, StatusFailed, reason); err != nil {
		return nil, fmt.Errorf("record rejection: %w", err)
	}
	return s.GetAttempt(ctx, id.String())
}

func (s *Service) GetAttempt(ctx context.Context, id string) (*Attempt, error) {
	uid, err := uuid.Parse(id)
	if err != nil {
		return nil, fmt.Errorf("%w: attempt id must be a UUID", ErrInvalid)
	}
	var a Attempt
	var stepsRaw []byte
	var approvalID *uuid.UUID
	err = s.db.QueryRow(ctx,
		`SELECT id, kind, source_env, target_env, artifact_digest, expected_generation,
		        policy_id, policy_version, evidence_id, evidence_version, approval_id,
		        status, failure_reason, steps, created_at, finished_at
		 FROM attempts WHERE id = $1`, uid).
		Scan(&a.ID, &a.Kind, &a.SourceEnv, &a.TargetEnv, &a.ArtifactDigest, &a.ExpectedGeneration,
			&a.PolicyID, &a.PolicyVersion, &a.EvidenceID, &a.EvidenceVersion, &approvalID,
			&a.Status, &a.FailureReason, &stepsRaw, &a.CreatedAt, &a.FinishedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, fmt.Errorf("attempt %s: %w", id, ErrNotFound)
	}
	if err != nil {
		return nil, err
	}
	if approvalID != nil {
		s := approvalID.String()
		a.ApprovalID = &s
	}
	a.Steps = []Step{}
	if err := json.Unmarshal(stepsRaw, &a.Steps); err != nil {
		return nil, fmt.Errorf("decode steps: %w", err)
	}
	return &a, nil
}

func (s *Service) ListAttempts(ctx context.Context, kind, status string) ([]Attempt, error) {
	query := `SELECT id, kind, source_env, target_env, artifact_digest, expected_generation,
	                 policy_id, policy_version, evidence_id, evidence_version, approval_id,
	                 status, failure_reason, steps, created_at, finished_at
	          FROM attempts`
	var args []any
	var where string
	if kind != "" {
		where += fmt.Sprintf(" AND kind = $%d", len(args)+1)
		args = append(args, kind)
	}
	if status != "" {
		where += fmt.Sprintf(" AND status = $%d", len(args)+1)
		args = append(args, status)
	}
	if where != "" {
		query += " WHERE " + where[5:]
	}
	query += " ORDER BY created_at DESC LIMIT 200"

	rows, err := s.db.Query(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []Attempt{}
	for rows.Next() {
		var a Attempt
		var stepsRaw []byte
		var approvalID *uuid.UUID
		if err := rows.Scan(&a.ID, &a.Kind, &a.SourceEnv, &a.TargetEnv, &a.ArtifactDigest, &a.ExpectedGeneration,
			&a.PolicyID, &a.PolicyVersion, &a.EvidenceID, &a.EvidenceVersion, &approvalID,
			&a.Status, &a.FailureReason, &stepsRaw, &a.CreatedAt, &a.FinishedAt); err != nil {
			return nil, err
		}
		if approvalID != nil {
			s := approvalID.String()
			a.ApprovalID = &s
		}
		a.Steps = []Step{}
		if err := json.Unmarshal(stepsRaw, &a.Steps); err != nil {
			return nil, err
		}
		out = append(out, a)
	}
	return out, rows.Err()
}

// ReconcileInterrupted marks attempts that were left 'running' (process
// crashed before the pointer commit finished) as 'interrupted'. Because the
// pointer switch and the attempt's success record commit in the same
// transaction, a 'running' attempt is proof the pointer never moved and the
// previous version is still live.
func (s *Service) ReconcileInterrupted(ctx context.Context, olderThan time.Duration) (int64, error) {
	step, _ := json.Marshal([]Step{{
		Name:   "reconciled",
		Detail: "attempt interrupted before pointer commit; previous version remains live",
		At:     s.now(),
	}})
	tag, err := s.db.Exec(ctx,
		`UPDATE attempts
		 SET status = $1, finished_at = now(), steps = steps || $2::jsonb
		 WHERE status = $3 AND created_at < now() - $4::interval`,
		StatusInterrupted, string(step), StatusRunning,
		fmt.Sprintf("%f seconds", olderThan.Seconds()))
	if err != nil {
		return 0, fmt.Errorf("reconcile interrupted: %w", err)
	}
	return tag.RowsAffected(), nil
}

// EnvHistory returns the pointer history with live completeness and
// retention evaluation for each entry (rollback candidates).
func (s *Service) EnvHistory(ctx context.Context, env string) ([]HistoryEntry, error) {
	e, err := s.GetEnvironment(ctx, env)
	if err != nil {
		return nil, err
	}
	rows, err := s.db.Query(ctx,
		`SELECT generation, digest, attempt_id, replaced_at,
		        ROW_NUMBER() OVER (ORDER BY generation DESC) AS rank
		 FROM env_history WHERE environment = $1
		 ORDER BY generation DESC`, env)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []HistoryEntry{}
	for rows.Next() {
		var h HistoryEntry
		var rank int64
		var attemptID uuid.UUID
		if err := rows.Scan(&h.Generation, &h.Digest, &attemptID, &h.ReplacedAt, &rank); err != nil {
			return nil, err
		}
		h.Environment = env
		h.AttemptID = attemptID.String()
		h.Current = e.CurrentDigest != nil && *e.CurrentDigest == h.Digest && e.Generation == h.Generation
		_, verr := s.blobs.Verify(env, h.Digest)
		h.Complete = verr == nil
		h.RetentionOK = rank <= int64(e.RetentionKeep) &&
			s.now().Sub(h.ReplacedAt) <= time.Duration(e.RetentionMaxAgeDays)*24*time.Hour
		out = append(out, h)
	}
	return out, rows.Err()
}
