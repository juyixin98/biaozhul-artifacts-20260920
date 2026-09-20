package models

import "time"

// Role 是系统内的用户角色。
type Role string

const (
	RoleDesigner Role = "designer"
	RolePM       Role = "pm"
	RoleReviewer Role = "reviewer"
)

type User struct {
	ID        uint64    `gorm:"primaryKey;autoIncrement" json:"id"`
	Name      string    `gorm:"size:64;not null;uniqueIndex" json:"name"`
	Role      Role      `gorm:"size:16;not null" json:"role"`
	CreatedAt time.Time `json:"created_at"`
}

// JobStatus 作业状态。
type JobStatus string

const (
	JobStatusInReview JobStatus = "in_review"
	JobStatusApproved JobStatus = "approved"
)

// Job 打样审查作业。LockVersion 用于对作业行的串行化守卫更新。
type Job struct {
	ID                uint64    `gorm:"primaryKey;autoIncrement" json:"id"`
	Title             string    `gorm:"size:128;not null" json:"title"`
	DesignerID        uint64    `gorm:"not null;index" json:"designer_id"`
	PMID              uint64    `gorm:"not null;index" json:"pm_id"`
	Status            JobStatus `gorm:"size:16;not null;default:in_review" json:"status"`
	CurrentRevisionID uint64    `gorm:"not null;default:0" json:"current_revision_id"`
	LockVersion       int64     `gorm:"not null;default:0" json:"-"`
	CreatedAt         time.Time `json:"created_at"`
	UpdatedAt         time.Time `json:"updated_at"`

	Designer     User                     `gorm:"foreignKey:DesignerID" json:"designer"`
	PM           User                     `gorm:"foreignKey:PMID" json:"pm"`
	Reviewers    []JobReviewer            `gorm:"foreignKey:JobID" json:"reviewers,omitempty"`
	TemplateList []ChecklistTemplateItem  `gorm:"foreignKey:JobID" json:"checklist_template,omitempty"`
}

// JobReviewer 作业指定的审查员（最多 8 名，由服务层校验）。
type JobReviewer struct {
	ID        uint64    `gorm:"primaryKey;autoIncrement" json:"-"`
	JobID     uint64    `gorm:"not null;uniqueIndex:uk_job_reviewer" json:"-"`
	UserID    uint64    `gorm:"not null;uniqueIndex:uk_job_reviewer" json:"user_id"`
	User      User      `gorm:"foreignKey:UserID" json:"user"`
	CreatedAt time.Time `json:"-"`
}

// ChecklistTemplateItem 作业级检查清单模板，每次提交修订时快照到该版本。
type ChecklistTemplateItem struct {
	ID      uint64 `gorm:"primaryKey;autoIncrement" json:"id"`
	JobID   uint64 `gorm:"not null;index" json:"-"`
	Seq     int    `gorm:"not null" json:"seq"`
	Content string `gorm:"size:512;not null" json:"content"`
}

// RevisionStatus 文件版本状态。
type RevisionStatus string

const (
	RevisionActive     RevisionStatus = "active"
	RevisionSuperseded RevisionStatus = "superseded"
)

// Revision 不可覆盖的文件版本。FilePath 为相对存储根的路径，由服务端生成。
type Revision struct {
	ID         uint64         `gorm:"primaryKey;autoIncrement" json:"id"`
	JobID      uint64         `gorm:"not null;index" json:"job_id"`
	VersionNo  int            `gorm:"not null" json:"version_no"`
	FilePath   string         `gorm:"size:255;not null;default:''" json:"-"`
	FileName   string         `gorm:"size:128;not null;default:''" json:"file_name"`
	Mime       string         `gorm:"size:64;not null;default:''" json:"mime"`
	Size       int64          `gorm:"not null;default:0" json:"size"`
	SHA256     string         `gorm:"size:64;not null;default:''" json:"sha256"`
	Status     RevisionStatus `gorm:"size:16;not null;default:active" json:"status"`
	UploadedBy uint64         `gorm:"not null" json:"uploaded_by"`
	CreatedAt  time.Time      `json:"created_at"`
}

// ChecklistStatus 检查项结论：仅 通过/失败/不适用，pending 表示未处理。
type ChecklistStatus string

const (
	ChecklistPending ChecklistStatus = "pending"
	ChecklistPass    ChecklistStatus = "pass"
	ChecklistFail    ChecklistStatus = "fail"
	ChecklistNA      ChecklistStatus = "na"
)

// ChecklistItem 绑定到某一文件版本的检查清单快照项。Version 为乐观锁。
type ChecklistItem struct {
	ID         uint64         `gorm:"primaryKey;autoIncrement" json:"id"`
	RevisionID uint64         `gorm:"not null;index" json:"revision_id"`
	Seq        int            `gorm:"not null" json:"seq"`
	Content    string         `gorm:"size:512;not null" json:"content"`
	Status     ChecklistStatus `gorm:"size:16;not null;default:pending" json:"status"`
	FailReason string         `gorm:"size:512;not null;default:''" json:"fail_reason,omitempty"`
	Version    int64          `gorm:"not null;default:1" json:"version"`
	UpdatedBy  uint64         `gorm:"not null;default:0" json:"updated_by"`
	UpdatedAt  time.Time      `json:"updated_at"`
}

// Comment 审查意见，绑定文件版本与审查员本人。Version 为乐观锁。
type Comment struct {
	ID         uint64    `gorm:"primaryKey;autoIncrement" json:"id"`
	RevisionID uint64    `gorm:"not null;index" json:"revision_id"`
	ReviewerID uint64    `gorm:"not null;index" json:"reviewer_id"`
	Content    string    `gorm:"size:2048;not null" json:"content"`
	Version    int64     `gorm:"not null;default:1" json:"version"`
	CreatedAt  time.Time `json:"created_at"`
	UpdatedAt  time.Time `json:"updated_at"`

	Reviewer User `gorm:"foreignKey:ReviewerID" json:"reviewer"`
}

// ReviewState 审查员对某版本的完成状态。
type ReviewState string

const (
	ReviewPending   ReviewState = "pending"
	ReviewCompleted ReviewState = "completed"
)

type ReviewerState struct {
	ID          uint64      `gorm:"primaryKey;autoIncrement" json:"-"`
	RevisionID  uint64      `gorm:"not null;uniqueIndex:uk_rev_reviewer" json:"revision_id"`
	ReviewerID  uint64      `gorm:"not null;uniqueIndex:uk_rev_reviewer" json:"reviewer_id"`
	State       ReviewState `gorm:"size:16;not null;default:pending" json:"state"`
	CompletedAt *time.Time  `json:"completed_at,omitempty"`

	Reviewer User `gorm:"foreignKey:ReviewerID" json:"reviewer"`
}

// Approval 最终签核记录，保存签核依据（文件摘要）。每个作业仅一条。
type Approval struct {
	ID         uint64    `gorm:"primaryKey;autoIncrement" json:"id"`
	JobID      uint64    `gorm:"not null;uniqueIndex" json:"job_id"`
	RevisionID uint64    `gorm:"not null" json:"revision_id"`
	ApprovedBy uint64    `gorm:"not null" json:"approved_by"`
	SHA256     string    `gorm:"size:64;not null" json:"sha256"`
	CreatedAt  time.Time `json:"created_at"`

	Approver User `gorm:"foreignKey:ApprovedBy" json:"approver"`
}

// SchemaMigration 记录已应用的迁移。
type SchemaMigration struct {
	ID        string    `gorm:"primaryKey;size:64"`
	AppliedAt time.Time `gorm:"autoCreateTime"`
}
