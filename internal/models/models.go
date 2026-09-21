package models

import (
	"time"

	"gorm.io/gorm"
)

// Case is a registered evidence case. The image file is never modified.
type Case struct {
	ID           uint           `json:"id" gorm:"primaryKey"`
	CaseRef      string         `json:"case_ref" gorm:"size:191;uniqueIndex;not null"`
	EvidenceFile string         `json:"evidence_file" gorm:"size:768;uniqueIndex;not null"` // whitelist-relative, symlink-resolved name
	ResolvedPath string         `json:"resolved_path" gorm:"size:1024;not null"`            // absolute path after symlink resolution
	Size         int64          `json:"size" gorm:"not null"`
	SHA256       string         `json:"sha256" gorm:"size:64;not null"` // immutable baseline digest
	MtimeNanos   int64          `json:"mtime_nanos" gorm:"not null"`    // file mtime captured at registration
	Device       uint64         `json:"device" gorm:"not null"`
	Inode        uint64         `json:"inode" gorm:"not null"`
	RegisteredBy string         `json:"registered_by" gorm:"size:191;not null"`
	RegisteredAt time.Time      `json:"registered_at"`
	CreatedAt    time.Time      `json:"created_at"`
	UpdatedAt    time.Time      `json:"updated_at"`
	DeletedAt    gorm.DeletedAt `json:"-" gorm:"index"`
}

func (Case) TableName() string { return "cases" }

// Job statuses.
const (
	JobQueued            = "queued"
	JobRunning           = "running"
	JobCompletedMatch    = "completed_match"    // recomputed digest equals baseline
	JobCompletedMismatch = "completed_mismatch" // recomputed digest differs from baseline
	JobFailed            = "failed"
)

// Error codes persisted on verification jobs.
const (
	ErrCodeIdentityMismatch = "IDENTITY_MISMATCH" // path/inode/size/mtime does not match registration
	ErrCodeContentModified  = "CONTENT_MODIFIED"  // an already processed chunk changed before resume
	ErrCodeBaselineMismatch = "BASELINE_MISMATCH" // final digest differs from baseline
	ErrCodeIO               = "IO_ERROR"
)

// VerificationJob is a persistent integrity-review job with resumable progress.
type VerificationJob struct {
	ID          uint       `json:"id" gorm:"primaryKey"`
	CaseID      uint       `json:"case_id" gorm:"index;not null"`
	Status      string     `json:"status" gorm:"size:32;index;not null"`
	Offset      int64      `json:"offset" gorm:"not null"` // bytes already hashed and persisted
	Size        int64      `json:"size" gorm:"not null"`
	ChunkSize   int        `json:"chunk_size" gorm:"not null"`
	ErrorCode   string     `json:"error_code" gorm:"size:64"`
	LastError   string     `json:"last_error" gorm:"size:1024"`
	Attempt     int        `json:"attempt" gorm:"not null;default:1"`
	TriggeredBy string     `json:"triggered_by" gorm:"size:191;not null"`
	StartedAt   *time.Time `json:"started_at"`
	FinishedAt  *time.Time `json:"finished_at"`
	CreatedAt   time.Time  `json:"created_at"`
	UpdatedAt   time.Time  `json:"updated_at"`
}

func (VerificationJob) TableName() string { return "verification_jobs" }

// JobChunk stores the digest of one processed chunk, proving exactly what has
// already been processed. On resume every stored chunk is re-verified.
type JobChunk struct {
	ID          uint   `gorm:"primaryKey"`
	JobID       uint   `gorm:"uniqueIndex:idx_job_chunk,priority:1;not null"`
	Idx         int    `gorm:"uniqueIndex:idx_job_chunk,priority:2;not null"`
	Offset      int64  `gorm:"not null"`
	Length      int64  `gorm:"not null"`
	ChunkSHA256 string `gorm:"size:64;not null"`
}

func (JobChunk) TableName() string { return "job_chunks" }

// Event types allowed on the evidence chain.
const (
	EventRegister     = "register"
	EventVerification = "verification"
	EventTransfer     = "transfer"
	EventNote         = "note"
)

// ChainEvent is one append-only evidence-chain entry.
type ChainEvent struct {
	ID            uint      `json:"-" gorm:"primaryKey"`
	CaseID        uint      `json:"case_id" gorm:"uniqueIndex:idx_case_seq,priority:1;index;not null"`
	Seq           int       `json:"seq" gorm:"uniqueIndex:idx_case_seq,priority:2;not null"` // per-case 1-based ordinal
	Type          string    `json:"type" gorm:"size:32;not null"`
	Actor         string    `json:"actor" gorm:"size:191;not null"`
	Payload       []byte    `json:"payload" gorm:"type:longtext;not null"` // canonicalized into ContentDigest
	PrevDigest    string    `json:"prev_digest" gorm:"size:64;not null"`
	ContentDigest string    `json:"content_digest" gorm:"size:64;not null"`
	EntryDigest   string    `json:"entry_digest" gorm:"size:64;not null"`
	CreatedAt     time.Time `json:"created_at"`
}

func (ChainEvent) TableName() string { return "chain_events" }

// ChainLock is an insert-on-key cross-database advisory lock that serializes
// concurrent chain appends for one case (works on MySQL InnoDB and SQLite).
type ChainLock struct {
	CaseID    uint      `gorm:"primaryKey"`
	Holder    string    `gorm:"size:191;not null"`
	ExpiresAt time.Time `gorm:"not null"`
	CreatedAt time.Time
}

func (ChainLock) TableName() string { return "chain_locks" }
