package models

import (
	"time"

	"gorm.io/datatypes"
)

// 事件类型
const (
	EventLogin        = "login"
	EventFileDownload = "file_download"
	EventUSB          = "usb"
)

// 告警状态
const (
	AlertNew           = "new"
	AlertInvestigating = "investigating"
	AlertEscalated     = "escalated"
	AlertResolved      = "resolved"
	AlertFalsePositive = "false_positive"
)

// 规则 key（v1 内置规则）
const (
	RuleDownloadBurst = "download_burst"
	RuleFirstUSB      = "first_usb"
	RuleNightActivity = "night_activity"
	RuleZScore        = "zscore_activity"
)

// Severity 等级
const (
	SeverityLow    = "low"
	SeverityMedium = "medium"
	SeverityHigh   = "high"
)

type Department struct {
	ID        string    `gorm:"primaryKey;size:26" json:"id"`
	Name      string    `gorm:"size:100;uniqueIndex" json:"name"`
	CreatedAt time.Time `json:"created_at"`
	UpdatedAt time.Time `json:"updated_at"`
}

func (Department) TableName() string { return "departments" }

type Employee struct {
	ID           string    `gorm:"primaryKey;size:26" json:"id"`
	DepartmentID string    `gorm:"size:26;column:department_id;index" json:"department_id"`
	Name         string    `gorm:"size:100" json:"name"`
	Email        string    `gorm:"size:190;uniqueIndex" json:"email"`
	Timezone     string    `gorm:"size:64" json:"timezone"`
	IsActive     bool      `gorm:"column:is_active" json:"is_active"`
	CreatedAt    time.Time `json:"created_at"`
	UpdatedAt    time.Time `json:"updated_at"`

	Department *Department `gorm:"foreignKey:DepartmentID" json:"department,omitempty"`
}

func (Employee) TableName() string { return "employees" }

type User struct {
	ID           string    `gorm:"primaryKey;size:26" json:"id"`
	Username     string    `gorm:"size:64;uniqueIndex" json:"username"`
	PasswordHash string    `gorm:"column:password_hash;size:255" json:"-"`
	Role         string    `gorm:"size:20" json:"role"`
	IsActive     bool      `json:"is_active"`
	CreatedAt    time.Time `json:"created_at"`
	UpdatedAt    time.Time `json:"updated_at"`

	Departments []Department `gorm:"many2many:user_departments;" json:"departments,omitempty"`
}

func (User) TableName() string { return "users" }

type UserDepartment struct {
	UserID       string    `gorm:"primaryKey;size:26;column:user_id"`
	DepartmentID string    `gorm:"primaryKey;size:26;column:department_id"`
	CreatedAt    time.Time `json:"created_at"`
}

func (UserDepartment) TableName() string { return "user_departments" }

type Event struct {
	ID          string         `gorm:"primaryKey;size:26" json:"id"`
	EventID     string         `gorm:"column:event_id;size:190;uniqueIndex" json:"event_id"`
	EventType   string         `gorm:"column:event_type;size:20" json:"event_type"`
	EmployeeID  string         `gorm:"column:employee_id;size:26" json:"employee_id"`
	OccurredAt  time.Time      `gorm:"column:occurred_at" json:"occurred_at"`
	ReceivedAt  time.Time      `gorm:"column:received_at" json:"received_at"`
	Metadata    datatypes.JSON `gorm:"type:json" json:"metadata"`
	ContentHash string         `gorm:"column:content_hash;size:64" json:"-"`
	// DetectedAt 为检测引擎处理完成时间；NULL 表示待处理（崩溃/重启后据此补算，不丢事件）。
	DetectedAt *time.Time `gorm:"column:detected_at" json:"detected_at,omitempty"`
}

func (Event) TableName() string { return "events" }

type DetectionRule struct {
	ID          string         `gorm:"primaryKey;size:26" json:"id"`
	RuleKey     string         `gorm:"column:rule_key;size:40" json:"rule_key"`
	Version     int            `json:"version"`
	Name        string         `gorm:"size:120" json:"name"`
	Description string         `gorm:"size:500" json:"description"`
	Params      datatypes.JSON `gorm:"type:json" json:"params"`
	IsActive    bool           `json:"is_active"`
	CreatedBy   *string        `gorm:"column:created_by;size:26" json:"created_by,omitempty"`
	CreatedAt   time.Time      `json:"created_at"`
}

func (DetectionRule) TableName() string { return "detection_rules" }

type Alert struct {
	ID             string         `gorm:"primaryKey;size:26" json:"id"`
	RuleKey        string         `gorm:"column:rule_key;size:40" json:"rule_key"`
	RuleVersion    int            `gorm:"column:rule_version" json:"rule_version"`
	EmployeeID     string         `gorm:"column:employee_id;size:26" json:"employee_id"`
	DepartmentID   string         `gorm:"column:department_id;size:26" json:"department_id"`
	Severity       string         `gorm:"size:10" json:"severity"`
	Status         string         `gorm:"size:20" json:"status"`
	Title          string         `gorm:"size:200" json:"title"`
	Summary        string         `gorm:"size:1000" json:"summary"`
	DedupKey       string         `gorm:"column:dedup_key;size:190;uniqueIndex" json:"dedup_key"`
	Evidence       datatypes.JSON `gorm:"type:json" json:"evidence"`
	WindowStart    *time.Time     `gorm:"column:window_start" json:"window_start,omitempty"`
	WindowEnd      *time.Time     `gorm:"column:window_end" json:"window_end,omitempty"`
	FirstSeenAt    time.Time      `gorm:"column:first_seen_at" json:"first_seen_at"`
	LastEventAt    time.Time      `gorm:"column:last_event_at" json:"last_event_at"`
	AcknowledgedAt *time.Time     `gorm:"column:acknowledged_at" json:"acknowledged_at,omitempty"`
	ResolvedAt     *time.Time     `gorm:"column:resolved_at" json:"resolved_at,omitempty"`
	AssignedTo     *string        `gorm:"column:assigned_to;size:26" json:"assigned_to,omitempty"`
	CreatedBy      *string        `gorm:"column:created_by;size:26" json:"created_by,omitempty"`
	CreatedAt      time.Time      `json:"created_at"`
	UpdatedAt      time.Time      `json:"updated_at"`

	Employee *Employee `gorm:"foreignKey:EmployeeID" json:"employee,omitempty"`
}

func (Alert) TableName() string { return "alerts" }

type AuditLog struct {
	ID         string         `gorm:"primaryKey;size:26" json:"id"`
	ActorID    *string        `gorm:"column:actor_id;size:26" json:"actor_id"`
	ActorName  string         `gorm:"column:actor_name;size:64" json:"actor_name"`
	Action     string         `gorm:"size:60" json:"action"`
	TargetType string         `gorm:"column:target_type;size:40" json:"target_type"`
	TargetID   string         `gorm:"column:target_id;size:190" json:"target_id"`
	Detail     datatypes.JSON `gorm:"type:json" json:"detail"`
	CreatedAt  time.Time      `json:"created_at"`
}

func (AuditLog) TableName() string { return "audit_logs" }

type SchedulerLock struct {
	Name        string    `gorm:"primaryKey;size:60" json:"name"`
	LockedBy    string    `gorm:"column:locked_by;size:120" json:"locked_by"`
	LockedAt    time.Time `gorm:"column:locked_at" json:"locked_at"`
	ExpiresAt   time.Time `gorm:"column:expires_at" json:"expires_at"`
	HeartbeatAt time.Time `gorm:"column:heartbeat_at" json:"heartbeat_at"`
}

func (SchedulerLock) TableName() string { return "scheduler_locks" }
