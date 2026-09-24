package store

import "time"

type Artifact struct {
	Digest      string    `json:"digest"`
	SizeBytes   int64     `json:"size_bytes"`
	StoragePath string    `json:"storage_path"`
	UploadedAt  time.Time `json:"uploaded_at"`
}

type EnvBlob struct {
	Env            string    `json:"env"`
	Digest         string    `json:"digest"`
	CopiedAt       time.Time `json:"copied_at"`
	VerifiedDigest string    `json:"verified_digest"`
}

type EvidenceRow struct {
	EvidenceID     string    `json:"evidence_id"`
	Version        int64     `json:"version"`
	ArtifactDigest string    `json:"artifact_digest"`
	TestsPassed    bool      `json:"tests_passed"`
	ResultJSON     string    `json:"result_json"`
	SignerKeyID    string    `json:"signer_key_id"`
	Signature      string    `json:"signature"`
	CreatedAt      time.Time `json:"created_at"`
}

type PolicyRow struct {
	PolicyID    string    `json:"policy_id"`
	Version     int64     `json:"version"`
	BodyJSON    string    `json:"body_json"`
	BodySHA256  string    `json:"body_sha256"`
	SignerKeyID string    `json:"signer_key_id"`
	Signature   string    `json:"signature"`
	CreatedAt   time.Time `json:"created_at"`
}

type TagRow struct {
	Name      string    `json:"name"`
	Digest    string    `json:"digest"`
	Gen       int64     `json:"gen"`
	UpdatedAt time.Time `json:"updated_at"`
}

type ApprovalRow struct {
	ApprovalID      string     `json:"approval_id"`
	PolicyID        string     `json:"policy_id"`
	PolicyVersion   int64      `json:"policy_version"`
	Env             string     `json:"env"`
	ArtifactDigest  string     `json:"artifact_digest"`
	EvidenceID      string     `json:"evidence_id"`
	EvidenceVersion int64      `json:"evidence_version"`
	ExpectedGen     int64      `json:"expected_gen"`
	SignerKeyID     string     `json:"signer_key_id"`
	Signature       string     `json:"signature"`
	Status          string     `json:"status"`
	ConsumedBy      *string    `json:"consumed_by"`
	CreatedAt       time.Time  `json:"created_at"`
	ConsumedAt      *time.Time `json:"consumed_at"`
}

type PointerRow struct {
	Env           string    `json:"env"`
	CurrentDigest *string   `json:"current_digest"`
	Gen           int64     `json:"gen"`
	UpdatedAt     time.Time `json:"updated_at"`
}

type HistoryRow struct {
	Env             string    `json:"env"`
	Gen             int64     `json:"gen"`
	Digest          string    `json:"digest"`
	ChangeType      string    `json:"change_type"`
	AttemptID       *string   `json:"attempt_id"`
	PolicyID        *string   `json:"policy_id"`
	PolicyVersion   *int64    `json:"policy_version"`
	EvidenceID      *string   `json:"evidence_id"`
	EvidenceVersion *int64    `json:"evidence_version"`
	CreatedAt       time.Time `json:"created_at"`
}

type AttemptRow struct {
	ID                   string     `json:"id"`
	Env                  string     `json:"env"`
	IdemKey              *string    `json:"idem_key"`
	RequestedDigest      string     `json:"requested_digest"`
	SourceEvidenceID     *string    `json:"source_evidence_id"`
	EvidenceVersion      *int64     `json:"evidence_version"`
	PolicyID             *string    `json:"policy_id"`
	PolicyVersion        *int64     `json:"policy_version"`
	ExpectedGen          int64      `json:"expected_gen"`
	ApprovalID           *string    `json:"approval_id"`
	Status               string     `json:"status"`
	FailureStage         *string    `json:"failure_stage"`
	FailureReason        *string    `json:"failure_reason"`
	CopySrcPath          *string    `json:"copy_src_path"`
	CopyDstPath          *string    `json:"copy_dst_path"`
	CopiedVerifiedDigest *string    `json:"copied_verified_digest"`
	GenBefore            *int64     `json:"gen_before"`
	GenAfter             *int64     `json:"gen_after"`
	ReceiptJSON          *string    `json:"receipt_json"`
	CreatedAt            time.Time  `json:"created_at"`
	FinishedAt           *time.Time `json:"finished_at"`
}

type GCRunRow struct {
	ID            int64     `json:"id"`
	PolicyID      string    `json:"policy_id"`
	PolicyVersion int64     `json:"policy_version"`
	KeepDigests   []string  `json:"keep_digests"`
	RemovedBlobs  []string  `json:"removed_blobs"`
	SkippedInuse  []string  `json:"skipped_inuse"`
	RanAt         time.Time `json:"ran_at"`
}
