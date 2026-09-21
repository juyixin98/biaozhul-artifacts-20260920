package models

import (
	"time"

	"gorm.io/gorm"
)

// Roles
const (
	RoleDesigner = "designer"
	RolePM       = "pm"
	RoleReviewer = "reviewer"
)

// File kinds
const (
	KindPDF = "pdf"
	KindPNG = "png"
)

// Review round status
const (
	RoundActive     = "active"     // open for opinions / approval on this version
	RoundSuperseded = "superseded" // a newer revision was submitted
	RoundApproved   = "approved"   // final sign-off recorded
)

// Item / opinion outcomes
const (
	OutcomePass    = "pass"
	OutcomeFail    = "fail"
	OutcomeNA      = "na"
	OutcomePending = "pending" // only for round items, never opinions
)

// Job workflow status
const (
	JobOpen     = "open"
	JobApproved = "approved"
)

type User struct {
	ID           uint           `gorm:"primaryKey" json:"id"`
	Username     string         `gorm:"size:64;uniqueIndex;not null" json:"username"`
	PasswordHash string         `gorm:"size:100;not null" json:"-"`
	Role         string         `gorm:"size:16;not null" json:"role"`
	Token        string         `gorm:"size:64;uniqueIndex" json:"-"`
	CreatedAt    time.Time      `json:"created_at"`
	UpdatedAt    time.Time      `json:"-"`
	DeletedAt    gorm.DeletedAt `gorm:"index" json:"-"`
}

type Job struct {
	ID          uint           `gorm:"primaryKey" json:"id"`
	Title       string         `gorm:"size:200;not null" json:"title"`
	Description string         `gorm:"size:2000" json:"description"`
	DesignerID  uint           `gorm:"not null;index" json:"designer_id"`
	PMID        uint           `gorm:"not null;index" json:"pm_id"`
	Status      string         `gorm:"size:16;not null;default:open" json:"status"`
	CreatedAt   time.Time      `json:"created_at"`
	UpdatedAt   time.Time      `json:"updated_at"`
	DeletedAt   gorm.DeletedAt `gorm:"index" json:"-"`

	Designer *User `gorm:"foreignKey:DesignerID" json:"designer,omitempty"`
	PM       *User `gorm:"foreignKey:PMID" json:"pm,omitempty"`
}

// JobReviewer is the immutable per-job reviewer roster (max 8).
type JobReviewer struct {
	ID        uint      `gorm:"primaryKey" json:"id"`
	JobID     uint      `gorm:"not null;uniqueIndex:uk_job_reviewer,priority:1" json:"job_id"`
	UserID    uint      `gorm:"not null;uniqueIndex:uk_job_reviewer,priority:2" json:"user_id"`
	CreatedAt time.Time `json:"created_at"`

	User *User `gorm:"foreignKey:UserID" json:"user,omitempty"`
}

type FileVersion struct {
	ID          uint      `gorm:"primaryKey" json:"id"`
	JobID       uint      `gorm:"not null;index" json:"job_id"`
	Version     int       `gorm:"not null;uniqueIndex:uk_job_version,priority:1" json:"version"`
	Filename    string    `gorm:"size:255;not null" json:"filename"` // original client filename
	Kind        string    `gorm:"size:8;not null" json:"kind"`
	Size        int64     `gorm:"not null" json:"size"`
	SHA256      string    `gorm:"size:64;not null" json:"sha256"`
	StoragePath string    `gorm:"size:500;not null;unique" json:"-"` // server-generated, relative to storage root
	UploadedBy  uint      `gorm:"not null" json:"uploaded_by"`
	CreatedAt   time.Time `json:"created_at"`
}

// ChecklistTemplateItem is the master checklist library seeded at startup.
type ChecklistTemplateItem struct {
	ID          uint      `gorm:"primaryKey" json:"id"`
	Code        string    `gorm:"size:32;uniqueIndex;not null" json:"code"`
	Description string    `gorm:"size:500;not null" json:"description"`
	Order       int       `gorm:"not null;default:0" json:"order"`
	CreatedAt   time.Time `json:"-"`
}

// Round is one review cycle, bound to one file version and one checklist snapshot.
type Round struct {
	ID               uint   `gorm:"primaryKey" json:"id"`
	JobID            uint   `gorm:"not null;uniqueIndex:uk_job_active,priority:1" json:"job_id"` // superseded rows flip status and release the partial index
	VersionID        uint   `gorm:"not null" json:"version_id"`
	VersionNumber    int    `gorm:"not null" json:"version_number"`
	ChecklistVersion string `gorm:"size:32;not null" json:"checklist_version"`
	Status           string `gorm:"size:16;not null;default:active" json:"status"`
	// ActiveFlag is 1 for the single live round of a job and NULL once the
	// round is superseded/approved. MySQL unique indexes allow multiple
	// NULLs, so (job_id, active_flag) enforces one live round per job.
	ActiveFlag *int       `gorm:"uniqueIndex:uk_job_active,priority:2" json:"-"`
	CreatedAt  time.Time  `json:"created_at"`
	ApprovedAt *time.Time `json:"approved_at,omitempty"`
}

// RoundItem is the frozen checklist snapshot entry for a round.
type RoundItem struct {
	ID          uint   `gorm:"primaryKey" json:"id"`
	RoundID     uint   `gorm:"not null;uniqueIndex:uk_round_item,priority:1" json:"round_id"`
	Code        string `gorm:"size:32;not null;uniqueIndex:uk_round_item,priority:2" json:"code"`
	Description string `gorm:"size:500;not null" json:"description"`
	SortOrder   int    `gorm:"not null;default:0" json:"sort_order"`
}

// Opinion is a reviewer's verdict on one checklist item of one round.
type Opinion struct {
	ID          uint      `gorm:"primaryKey" json:"id"`
	RoundID     uint      `gorm:"not null;uniqueIndex:uk_opinion,priority:1" json:"round_id"`
	ReviewerID  uint      `gorm:"not null;uniqueIndex:uk_opinion,priority:2" json:"reviewer_id"`
	RoundItemID uint      `gorm:"not null;uniqueIndex:uk_opinion,priority:3" json:"round_item_id"`
	Outcome     string    `gorm:"size:8;not null" json:"outcome"`
	Reason      string    `gorm:"size:1000" json:"reason"`
	Version     int       `gorm:"not null;default:1" json:"version"` // optimistic-lock version
	CreatedAt   time.Time `json:"created_at"`
	UpdatedAt   time.Time `json:"updated_at"`

	Reviewer *User `gorm:"foreignKey:ReviewerID" json:"reviewer,omitempty"`
}

// Approval records the final sign-off on a round.
type Approval struct {
	ID         uint      `gorm:"primaryKey" json:"id"`
	RoundID    uint      `gorm:"not null;uniqueIndex" json:"round_id"`
	JobID      uint      `gorm:"not null;uniqueIndex:uk_job_approval" json:"job_id"`
	ApproverID uint      `gorm:"not null" json:"approver_id"`
	Basis      string    `gorm:"type:text" json:"basis"` // frozen summary of the sign-off basis
	CreatedAt  time.Time `json:"created_at"`

	Approver *User `gorm:"foreignKey:ApproverID" json:"approver,omitempty"`
}

// SchemaMigration tracks applied SQL migrations.
type SchemaMigration struct {
	Version   string    `gorm:"primaryKey;size:32"`
	AppliedAt time.Time `gorm:"not null"`
}
