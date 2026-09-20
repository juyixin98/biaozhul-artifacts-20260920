package model

import "time"

// Role constants.
const (
	RoleDesigner = "designer"
	RolePM       = "pm"
	RoleReviewer = "reviewer"
)

// Job lifecycle statuses.
const (
	JobStatusInReview = "in_review"
	JobStatusApproved = "approved"
)

// Version (file version) statuses.
const (
	VersionStatusSuperseded = "superseded" // a newer revision exists
	VersionStatusReady      = "ready"      // current, waiting for review/sign-off
	VersionStatusApproved   = "approved"   // carried by the approved job
)

// Review round statuses (mirror the bound version).
const (
	RoundStatusOpen       = "open"
	RoundStatusSuperseded = "superseded"
	RoundStatusApproved   = "approved"
)

// Review item verdicts.
const (
	VerdictPass     = "pass"
	VerdictFail     = "fail"
	VerdictNA       = "na"
	VerdictPending  = "pending" // no opinion recorded yet (computed, not stored)
	VerdictResolved = "resolved"
)

// User is an authenticated principal. The static API token grants access and
// identifies the caller; passwords/OAuth are deliberately out of scope.
type User struct {
	ID        int64     `gorm:"column:id;primaryKey;autoIncrement" json:"id"`
	Name      string    `gorm:"column:name;type:varchar(100);not null" json:"name"`
	Role      string    `gorm:"column:role;type:varchar(20);not null" json:"role"`
	APIToken  string    `gorm:"column:api_token;type:varchar(128);not null;uniqueIndex" json:"-"`
	CreatedAt time.Time `gorm:"column:created_at;not null" json:"created_at"`
}

func (User) TableName() string { return "users" }

// Job groups designer, project manager, reviewers and all revisions.
type Job struct {
	ID             int64     `gorm:"column:id;primaryKey;autoIncrement" json:"id"`
	Name           string    `gorm:"column:name;type:varchar(200);not null;uniqueIndex:uniq_job_designer_name,priority:2" json:"name"`
	DesignerID     int64     `gorm:"column:designer_id;not null;uniqueIndex:uniq_job_designer_name,priority:1" json:"designer_id"`
	PMID           int64     `gorm:"column:pm_id;not null" json:"pm_id"`
	Status         string    `gorm:"column:status;type:varchar(20);not null;default:in_review" json:"status"`
	CurrentVersion int       `gorm:"column:current_version;not null;default:0" json:"current_version"`
	CreatedAt      time.Time `gorm:"column:created_at;not null" json:"created_at"`
	UpdatedAt      time.Time `gorm:"column:updated_at;not null" json:"updated_at"`
}

func (Job) TableName() string { return "jobs" }

// JobReviewer lists the at-most-8 designated reviewers of a job. Assignment is
// immutable: final sign-off always requires exactly these reviewers.
type JobReviewer struct {
	JobID      int64     `gorm:"column:job_id;primaryKey" json:"job_id"`
	ReviewerID int64     `gorm:"column:reviewer_id;primaryKey" json:"reviewer_id"`
	CreatedAt  time.Time `gorm:"column:created_at;not null" json:"created_at"`
}

func (JobReviewer) TableName() string { return "job_reviewers" }

// JobChecklistItem is the job's checklist template. Every new review round
// copies it into an immutable snapshot, so later edits never rewrite history.
type JobChecklistItem struct {
	ID          int64  `gorm:"column:id;primaryKey;autoIncrement" json:"id"`
	JobID       int64  `gorm:"column:job_id;not null;index:idx_job_checklist,priority:1" json:"job_id"`
	ItemOrder   int    `gorm:"column:item_order;not null;index:idx_job_checklist,priority:2" json:"item_order"`
	Code        string `gorm:"column:code;type:varchar(40);not null" json:"code"`
	Description string `gorm:"column:description;type:varchar(500);not null" json:"description"`
}

func (JobChecklistItem) TableName() string { return "job_checklist_items" }

// FileVersion is one immutable revision of the design file. A row becomes
// visible atomically with its on-disk file inside one job-locked transaction;
// startup recovery garbage-collects any unreferenced file.
type FileVersion struct {
	ID         int64     `gorm:"column:id;primaryKey;autoIncrement" json:"id"`
	JobID      int64     `gorm:"column:job_id;not null;index:idx_version_job,priority:1;uniqueIndex:uniq_job_version,priority:1" json:"job_id"`
	Version    int       `gorm:"column:version;not null;uniqueIndex:uniq_job_version,priority:2" json:"version"`
	FileName   string    `gorm:"column:file_name;type:varchar(255);not null" json:"file_name"`
	StoredPath string    `gorm:"column:stored_path;type:varchar(512);not null" json:"stored_path"`
	SHA256     string    `gorm:"column:sha256;type:char(64);not null" json:"sha256"`
	SizeBytes  int64     `gorm:"column:size_bytes;not null" json:"size_bytes"`
	MimeType   string    `gorm:"column:mime_type;type:varchar(80);not null" json:"mime_type"`
	UploadedBy int64     `gorm:"column:uploaded_by;not null" json:"uploaded_by"`
	Status     string    `gorm:"column:status;type:varchar(20);not null;default:ready;index:idx_version_job,priority:2" json:"status"`
	CreatedAt  time.Time `gorm:"column:created_at;not null" json:"created_at"`
}

func (FileVersion) TableName() string { return "file_versions" }

// ReviewRound binds one checklist snapshot to one file version.
type ReviewRound struct {
	ID           int64      `gorm:"column:id;primaryKey;autoIncrement" json:"id"`
	JobID        int64      `gorm:"column:job_id;not null;index" json:"job_id"`
	Version      int        `gorm:"column:version;not null;uniqueIndex:uniq_round_job_version,priority:2" json:"version"`
	FileVerID    int64      `gorm:"column:file_version_id;not null" json:"file_version_id"`
	Status       string     `gorm:"column:status;type:varchar(20);not null;default:open" json:"status"`
	SignoffBy    *int64     `gorm:"column:signoff_by" json:"signoff_by,omitempty"`
	SignoffAt    *time.Time `gorm:"column:signoff_at" json:"signoff_at,omitempty"`
	SignoffBasis string     `gorm:"column:signoff_basis;type:text" json:"signoff_basis,omitempty"`
	CreatedAt    time.Time  `gorm:"column:created_at;not null" json:"created_at"`
}

func (ReviewRound) TableName() string { return "review_rounds" }

// ChecklistItem is one item of a round's immutable checklist snapshot.
type ChecklistItem struct {
	ID          int64  `gorm:"column:id;primaryKey;autoIncrement" json:"id"`
	RoundID     int64  `gorm:"column:round_id;not null;index:idx_checklist_round,priority:1" json:"round_id"`
	ItemOrder   int    `gorm:"column:item_order;not null;index:idx_checklist_round,priority:2" json:"item_order"`
	Code        string `gorm:"column:code;type:varchar(40);not null" json:"code"`
	Description string `gorm:"column:description;type:varchar(500);not null" json:"description"`
}

func (ChecklistItem) TableName() string { return "checklist_items" }

// Opinion is one designated reviewer's verdict for one checklist item of one
// round. It is keyed by (round, reviewer, checklist item): a reviewer submits
// exactly one opinion per item. RowVersion implements optimistic concurrency.
type Opinion struct {
	ID              int64     `gorm:"column:id;primaryKey;autoIncrement" json:"id"`
	RoundID         int64     `gorm:"column:round_id;not null;uniqueIndex:uniq_opinion,priority:1" json:"round_id"`
	ReviewerID      int64     `gorm:"column:reviewer_id;not null;uniqueIndex:uniq_opinion,priority:2" json:"reviewer_id"`
	ChecklistItemID int64     `gorm:"column:checklist_item_id;not null;uniqueIndex:uniq_opinion,priority:3" json:"checklist_item_id"`
	Verdict         string    `gorm:"column:verdict;type:varchar(20);not null" json:"verdict"`
	Reason          string    `gorm:"column:reason;type:varchar(1000)" json:"reason"`
	RowVersion      int       `gorm:"column:row_version;not null;default:1" json:"row_version"`
	CreatedAt       time.Time `gorm:"column:created_at;not null" json:"created_at"`
	UpdatedAt       time.Time `gorm:"column:updated_at;not null" json:"updated_at"`
}

func (Opinion) TableName() string { return "opinions" }

// Signoff is retained for a complete audit trail; one row per approved round.
type Signoff struct {
	ID        int64     `gorm:"column:id;primaryKey;autoIncrement" json:"id"`
	RoundID   int64     `gorm:"column:round_id;not null;uniqueIndex" json:"round_id"`
	JobID     int64     `gorm:"column:job_id;not null;index" json:"job_id"`
	Version   int       `gorm:"column:version;not null" json:"version"`
	PMUserID  int64     `gorm:"column:pm_user_id;not null" json:"pm_user_id"`
	Basis     string    `gorm:"column:basis;type:text;not null" json:"basis"`
	CreatedAt time.Time `gorm:"column:created_at;not null" json:"created_at"`
}

func (Signoff) TableName() string { return "signoffs" }

// SchemaMigration records applied SQL migrations.
type SchemaMigration struct {
	Version   string    `gorm:"column:version;primaryKey" json:"version"`
	AppliedAt time.Time `gorm:"column:applied_at;not null" json:"applied_at"`
}

func (SchemaMigration) TableName() string { return "schema_migrations" }
