// Package domain contains GORM entities and domain enums for ProofCycle.
package domain

import (
	"time"
)

// Role 标识用户在系统中的全局身份。作业内的具体分工以 JobMember / JobReviewer 为准。
type Role string

const (
	RoleDesigner Role = "designer" // 设计师：上传修订，不能批准自己的作业
	RolePM       Role = "pm"       // 项目经理：创建作业、最终签核
	RoleReviewer Role = "reviewer" // 审查员：提交检查意见
)

func (r Role) Valid() bool {
	switch r {
	case RoleDesigner, RolePM, RoleReviewer:
		return true
	}
	return false
}

// JobStatus 是作业的整体状态。
type JobStatus string

const (
	StatusInReview JobStatus = "in_review" // 当前版本待签核
	StatusApproved JobStatus = "approved"  // 当前版本已签核；上传新修订后回到 in_review
)

// ItemResult 是检查项结论。
type ItemResult string

const (
	ResultPass          ItemResult = "pass" // 通过
	ResultFail          ItemResult = "fail" // 失败：必须填写原因
	ResultNotApplicable ItemResult = "na"   // 不适用
)

func (r ItemResult) Valid() bool {
	return r == ResultPass || r == ResultFail || r == ResultNotApplicable
}

// User 系统用户。角色为默认身份；作业级权限由成员表决定。
type User struct {
	ID        string `gorm:"primaryKey;size:32"`
	Name      string `gorm:"size:64;not null"`
	Role      Role   `gorm:"size:16;not null;index"`
	CreatedAt time.Time
}

// Job 包装打样审查作业。
type Job struct {
	ID               string    `gorm:"primaryKey;size:32"`
	Name             string    `gorm:"size:200;not null"`
	Description      string    `gorm:"type:text"`
	DesignerID       string    `gorm:"size:32;not null;index"`
	PMID             string    `gorm:"size:32;not null;index"`
	ChecklistID      string    `gorm:"size:32;not null;index"`
	Status           JobStatus `gorm:"size:16;not null;default:in_review"`
	CurrentVersionNo int       `gorm:"not null;default:0"` // 0 表示尚未上传任何文件
	CreatedAt        time.Time
	UpdatedAt        time.Time

	Designer *User `gorm:"foreignKey:DesignerID"`
	PM       *User `gorm:"foreignKey:PMID"`
}

// JobReviewer 作业指定的审查员，最多 8 名。
type JobReviewer struct {
	ID        string `gorm:"primaryKey;size:32"`
	JobID     string `gorm:"size:32;not null;uniqueIndex:uk_job_reviewer,priority:1;index"`
	UserID    string `gorm:"size:32;not null;uniqueIndex:uk_job_reviewer,priority:2;index"`
	CreatedAt time.Time

	User *User `gorm:"foreignKey:UserID"`
	Job  *Job  `gorm:"foreignKey:JobID"`
}

// Checklist 检查清单模板。作业创建时绑定，每个文件版本生成一份不可变快照。
type Checklist struct {
	ID        string `gorm:"primaryKey;size:32"`
	Name      string `gorm:"size:200;not null"`
	Version   int    `gorm:"not null;default:1"`
	CreatedAt time.Time
	Items     []ChecklistItem `gorm:"foreignKey:ChecklistID"`
}

// ChecklistItem 清单项模板。
type ChecklistItem struct {
	ID          string `gorm:"primaryKey;size:32"`
	ChecklistID string `gorm:"size:32;not null;index"`
	OrderNo     int    `gorm:"not null"`
	Code        string `gorm:"size:32;not null"`
	Text        string `gorm:"size:500;not null"`
}

// FileVersion 一轮审查绑定的文件版本。修订文件永不覆盖，逐版本新增。
type FileVersion struct {
	ID           string `gorm:"primaryKey;size:32"`
	JobID        string `gorm:"size:32;not null;uniqueIndex:uk_job_verno,priority:1;index"`
	VersionNo    int    `gorm:"not null;uniqueIndex:uk_job_verno,priority:2"`
	UploadedByID string `gorm:"size:32;not null;index"`
	FileName     string `gorm:"size:255;not null"` // 上传时的原始文件名
	StoredPath   string `gorm:"size:500;not null"` // 服务生成、相对存储根目录
	ContentType  string `gorm:"size:64;not null"`  // application/pdf | image/png
	SizeBytes    int64  `gorm:"not null"`
	SHA256       string `gorm:"size:64;not null;index"`
	SnapshotID   string `gorm:"size:32;not null"`
	// SupersededByID 指向取代本版本的新版本；允许对已批准版本继续提修订。
	SupersededByID *string `gorm:"size:32;index"`
	CreatedAt      time.Time

	UploadedBy *User              `gorm:"foreignKey:UploadedByID"`
	Snapshot   *ChecklistSnapshot `gorm:"foreignKey:SnapshotID"`
}

// ChecklistSnapshot 文件版本对应的清单快照，保证审查结论始终绑定当时清单。
type ChecklistSnapshot struct {
	ID            string `gorm:"primaryKey;size:32"`
	JobID         string `gorm:"size:32;not null;index"`
	VersionID     string `gorm:"size:32;not null;uniqueIndex:uk_snapshot_version"`
	ChecklistID   string `gorm:"size:32;not null"`
	ChecklistName string `gorm:"size:200;not null"`
	SourceVersion int    `gorm:"not null"`
	CreatedAt     time.Time
	Items         []ChecklistSnapshotItem `gorm:"foreignKey:SnapshotID"`
}

// ChecklistSnapshotItem 快照项，内容复制自模板且此后不可变。
type ChecklistSnapshotItem struct {
	ID         string `gorm:"primaryKey;size:32"`
	SnapshotID string `gorm:"size:32;not null;index"`
	OrderNo    int    `gorm:"not null;index:idx_snapshot_order,priority:2"`
	Code       string `gorm:"size:32;not null"`
	Text       string `gorm:"size:500;not null"`
}

// Review 一名审查员针对一个文件版本的一份意见。审查员每版本至多一份。
type Review struct {
	ID         string `gorm:"primaryKey;size:32"`
	VersionID  string `gorm:"size:32;not null;uniqueIndex:uk_version_reviewer,priority:1;index"`
	ReviewerID string `gorm:"size:32;not null;uniqueIndex:uk_version_reviewer,priority:2;index"`
	// RowVersion 乐观锁：每次修改递增，更新意见必须携带预期版本，冲突明确拒绝(409)。
	RowVersion  int `gorm:"not null;default:1"`
	SubmittedAt time.Time
	UpdatedAt   time.Time
	Items       []ReviewItem `gorm:"foreignKey:ReviewID"`
}

// ReviewItem 审查员对单个快照项的结论。
type ReviewItem struct {
	ID             string     `gorm:"primaryKey;size:32"`
	ReviewID       string     `gorm:"size:32;not null;uniqueIndex:uk_review_item,priority:1;index"`
	SnapshotItemID string     `gorm:"size:32;not null;uniqueIndex:uk_review_item,priority:2"`
	Result         ItemResult `gorm:"size:8;not null"`
	FailReason     string     `gorm:"type:text"` // result=fail 时必填
}

// Approval 最终签核记录。每个文件版本至多一条（数据库唯一约束兜底，防并发双签）。
type Approval struct {
	ID         string `gorm:"primaryKey;size:32"`
	JobID      string `gorm:"size:32;not null;uniqueIndex:uk_approval_version,priority:1;index"`
	VersionID  string `gorm:"size:32;not null;uniqueIndex:uk_approval_version,priority:2"`
	ApproverID string `gorm:"size:32;not null;index"`
	// Basis 记录签核依据：完成审查的审查员、无失败项等，随报告导出。
	Basis     string `gorm:"type:text;not null"`
	CreatedAt time.Time

	Approver *User `gorm:"foreignKey:ApproverID"`
}
