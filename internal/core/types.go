// Package core implements the artifact-promotion engine: atomic promotion
// from test to staging with immutable digest references, optimistic
// concurrency on environment generations, and full per-attempt evidence.
package core

import "time"

// Attempt statuses.
const (
	StatusRunning     = "running"
	StatusSucceeded   = "succeeded"
	StatusFailed      = "failed"
	StatusInterrupted = "interrupted"
)

// Attempt kinds.
const (
	KindIngest    = "ingest"
	KindPromotion = "promotion"
	KindRollback  = "rollback"
)

// Step is one entry in an attempt's evidence log. Steps are appended to the
// attempts row as they happen, so evidence survives a crash at any point.
type Step struct {
	Name   string    `json:"name"`
	Detail string    `json:"detail,omitempty"`
	At     time.Time `json:"at"`
}

type Environment struct {
	Name                string    `json:"name"`
	CurrentDigest       *string   `json:"current_digest"`
	Generation          int64     `json:"generation"`
	RetentionKeep       int       `json:"retention_keep"`
	RetentionMaxAgeDays int       `json:"retention_max_age_days"`
	CreatedAt           time.Time `json:"created_at"`
	UpdatedAt           time.Time `json:"updated_at"`
}

type Evidence struct {
	ID             string         `json:"id"`
	Version        int            `json:"version"`
	ArtifactDigest string         `json:"artifact_digest"`
	Suite          string         `json:"suite"`
	Passed         bool           `json:"passed"`
	Report         map[string]any `json:"report"`
	CreatedAt      time.Time      `json:"created_at"`
}

type Policy struct {
	ID                 string         `json:"id"`
	Version            int            `json:"version"`
	SourceEnv          string         `json:"source_env"`
	TargetEnv          string         `json:"target_env"`
	RequiredSuite      string         `json:"required_suite"`
	MinEvidenceVersion int            `json:"min_evidence_version"`
	Body               map[string]any `json:"body"`
	CreatedAt          time.Time      `json:"created_at"`
}

type Approval struct {
	ID             string     `json:"id"`
	Environment    string     `json:"environment"`
	Generation     int64      `json:"generation"`
	ArtifactDigest string     `json:"artifact_digest"`
	Approver       string     `json:"approver"`
	CreatedAt      time.Time  `json:"created_at"`
	ConsumedAt     *time.Time `json:"consumed_at,omitempty"`
}

type Attempt struct {
	ID                 string     `json:"id"`
	Kind               string     `json:"kind"`
	SourceEnv          string     `json:"source_env"`
	TargetEnv          string     `json:"target_env"`
	ArtifactDigest     string     `json:"artifact_digest"`
	ExpectedGeneration int64      `json:"expected_generation"`
	PolicyID           *string    `json:"policy_id,omitempty"`
	PolicyVersion      *int       `json:"policy_version,omitempty"`
	EvidenceID         *string    `json:"evidence_id,omitempty"`
	EvidenceVersion    *int       `json:"evidence_version,omitempty"`
	ApprovalID         *string    `json:"approval_id,omitempty"`
	Status             string     `json:"status"`
	FailureReason      *string    `json:"failure_reason,omitempty"`
	Steps              []Step     `json:"steps"`
	CreatedAt          time.Time  `json:"created_at"`
	FinishedAt         *time.Time `json:"finished_at,omitempty"`
}

type HistoryEntry struct {
	Environment string    `json:"environment"`
	Generation  int64     `json:"generation"`
	Digest      string    `json:"digest"`
	AttemptID   string    `json:"attempt_id"`
	ReplacedAt  time.Time `json:"replaced_at"`
	Current     bool      `json:"current"`
	Complete    bool      `json:"complete"`     // blob present and sha256-verified
	RetentionOK bool      `json:"retention_ok"` // within retention rules, may be a rollback target
}

// PromoteRequest references everything by immutable, versioned identifiers:
// the artifact by digest, the evidence and policy by (id, version), and the
// target environment by its expected generation. No floating tags exist.
type PromoteRequest struct {
	SourceEnv          string `json:"source_env"`
	TargetEnv          string `json:"target_env"`
	Digest             string `json:"digest"`
	EvidenceID         string `json:"evidence_id"`
	EvidenceVersion    int    `json:"evidence_version"`
	PolicyID           string `json:"policy_id"`
	PolicyVersion      int    `json:"policy_version"`
	ApprovalID         string `json:"approval_id"`
	ExpectedGeneration int64  `json:"expected_generation"`
}

type RollbackRequest struct {
	Environment        string `json:"environment"`
	Digest             string `json:"digest"`
	ExpectedGeneration int64  `json:"expected_generation"`
}
