// Package domain contains the persisted entities shared across services.
package domain

import (
	"time"
)

// Case is an investigation case. The genesis hash anchors its hash chain.
type Case struct {
	ID          string    `gorm:"primaryKey;size:64" json:"id"`
	Name        string    `gorm:"size:255;not null" json:"name"`
	Description string    `gorm:"type:text" json:"description"`
	GenesisHash string    `gorm:"size:64;not null" json:"genesis_hash"`
	CreatedAt   time.Time `json:"created_at"`
	UpdatedAt   time.Time `json:"updated_at"`
}

// Evidence is a registered raw/dd disk image baseline.
//
// Baseline fields (size + sha256) are only persisted after the whole file was
// hashed twice and verified unchanged in between, so a wrong baseline can
// never be saved.
type Evidence struct {
	ID            string    `gorm:"primaryKey;size:64" json:"id"`
	CaseID        string    `gorm:"size:64;index:idx_evidence_case_path,unique;not null" json:"case_id"`
	SourcePath    string    `gorm:"size:700;index:idx_evidence_case_path,unique;not null" json:"source_path"`
	RealPath      string    `gorm:"size:1024;not null" json:"real_path"`
	Filename      string    `gorm:"size:255;not null" json:"filename"`
	Size          int64     `gorm:"not null" json:"size"`
	SHA256        string    `gorm:"size:64;not null" json:"sha256"`
	MimeType      string    `gorm:"size:128" json:"mime_type"`
	FileMode      uint32    `json:"file_mode"`
	DeviceID      uint64    `json:"device_id"`
	Inode         uint64    `json:"inode"`
	Custodian     string    `gorm:"size:255" json:"custodian"`
	RegisteredBy  string    `gorm:"size:255;not null" json:"registered_by"`
	RegisteredSeq int64     `gorm:"not null" json:"registered_seq"`
	CreatedAt     time.Time `json:"created_at"`
	UpdatedAt     time.Time `json:"updated_at"`
}

// ReviewStatus values for ReviewJob.
const (
	ReviewRunning     = "running"
	ReviewInterrupted = "interrupted"
	ReviewCompleted   = "completed"
	ReviewFailed      = "failed"
)

// ResumableStatuses are states from which a job may continue.
var ResumableStatuses = []string{ReviewRunning, ReviewInterrupted, ReviewFailed}

// ReviewJob is a resumable integrity re-check persisted across restarts.
type ReviewJob struct {
	ID               string     `gorm:"primaryKey;size:64" json:"id"`
	EvidenceID       string     `gorm:"size:64;index;not null" json:"evidence_id"`
	CaseID           string     `gorm:"size:64;index;not null" json:"case_id"`
	Status           string     `gorm:"size:16;not null" json:"status"`
	Offset           int64      `gorm:"not null;default:0" json:"offset"`
	PrefixSHA256     string     `gorm:"size:64" json:"prefix_sha256"`
	FinalSHA256      string     `gorm:"size:64" json:"final_sha256"`
	BaselineSHA256   string     `gorm:"size:64;not null" json:"baseline_sha256"`
	ExpectedSize     int64      `gorm:"not null" json:"expected_size"`
	ExpectedRealPath string     `gorm:"size:1024;not null" json:"expected_real_path"`
	ExpectedDeviceID uint64     `json:"expected_device_id"`
	ExpectedInode    uint64     `json:"expected_inode"`
	LastChunkTime    *time.Time `json:"last_chunk_time"`
	Result           string     `gorm:"type:text" json:"result"`
	ErrorMessage     string     `gorm:"type:text" json:"error_message"`
	StartedBy        string     `gorm:"size:255;not null" json:"started_by"`
	ChainSeq         int64      `json:"chain_seq"`
	CreatedAt        time.Time  `json:"created_at"`
	UpdatedAt        time.Time  `json:"updated_at"`
	CompletedAt      *time.Time `json:"completed_at"`
}

// Chain event types.
const (
	EventCaseCreated = "case_created"
	EventRegistered  = "registered"
	EventReview      = "review"
	EventTransfer    = "transfer"
	EventNote        = "note"
)

// ChainEvent is one append-only link of a case's hash chain.
//
// ContentJSON is the canonical (deterministic) serialized payload; Digest is
// SHA256(PrevDigest || ContentJSON). The (case_id, seq) unique index makes a
// concurrent fork impossible even with multiple server processes.
type ChainEvent struct {
	ID          string    `gorm:"primaryKey;size:64" json:"id"`
	CaseID      string    `gorm:"size:64;index:idx_chain_case_seq,unique;not null" json:"case_id"`
	Seq         int64     `gorm:"index:idx_chain_case_seq,unique;not null" json:"seq"`
	EventType   string    `gorm:"size:32;not null" json:"event_type"`
	Actor       string    `gorm:"size:255;not null" json:"actor"`
	PrevDigest  string    `gorm:"size:64;not null" json:"prev_digest"`
	ContentJSON string    `gorm:"type:text;not null" json:"content_json"`
	Digest      string    `gorm:"size:64;not null;index" json:"digest"`
	CreatedAt   time.Time `gorm:"not null" json:"created_at"`
}

// AllModels returns every GORM-managed entity for AutoMigrate.
func AllModels() []any {
	return []any{&Case{}, &Evidence{}, &ReviewJob{}, &ChainEvent{}}
}
