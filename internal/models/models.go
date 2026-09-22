package models

import (
	"database/sql"
	"database/sql/driver"
	"encoding/json"
	"errors"
	"time"
)

// Event types.
const (
	EventTypeLogin        = "login"
	EventTypeFileDownload = "file_download"
	EventTypeUSB          = "usb"
)

// Alert statuses.
const (
	AlertStatusNew           = "new"
	AlertStatusInvestigating = "investigating"
	AlertStatusEscalated     = "escalated"
	AlertStatusResolved      = "resolved"
	AlertStatusFalsePositive = "false_positive"
)

// Rule codes.
const (
	RuleDownloadBurst = "download_burst"
	RuleNightActivity = "night_activity"
	RuleFirstUSB      = "first_usb"
	RuleStatistical   = "statistical"
)

// Window scopes evaluated by the detection engine.
const (
	ScopeBurst    = "burst"
	ScopeNight    = "night"
	ScopeFirstUSB = "firstusb"
	ScopeStat     = "stat"
)

// Roles.
const (
	RoleAdmin   = "admin"
	RoleAnalyst = "analyst"
)

// JSONMap stores a JSON object in a MySQL JSON column without pulling in
// gorm.io/datatypes.
type JSONMap map[string]any

func (m JSONMap) Value() (driver.Value, error) {
	if m == nil {
		return nil, nil
	}
	b, err := json.Marshal(m)
	if err != nil {
		return nil, err
	}
	return string(b), nil
}

func (m *JSONMap) Scan(src any) error {
	if src == nil {
		*m = nil
		return nil
	}
	var b []byte
	switch v := src.(type) {
	case []byte:
		b = v
	case string:
		b = []byte(v)
	default:
		return errors.New("incompatible type for JSONMap")
	}
	if len(b) == 0 {
		*m = nil
		return nil
	}
	return json.Unmarshal(b, m)
}

type Department struct {
	ID        uint64    `gorm:"primaryKey" json:"id"`
	Name      string    `gorm:"size:128;uniqueIndex" json:"name"`
	CreatedAt time.Time `json:"created_at"`
}

func (Department) TableName() string { return "departments" }

type Employee struct {
	ID        uint64    `gorm:"primaryKey" json:"id"`
	Name      string    `gorm:"size:128" json:"name"`
	DeptID    uint64    `gorm:"column:dept_id" json:"dept_id"`
	TimeZone  string    `gorm:"column:time_zone;size:64" json:"time_zone"`
	CreatedAt time.Time `json:"created_at"`
}

func (Employee) TableName() string { return "employees" }

type APIKey struct {
	ID        uint64      `gorm:"primaryKey" json:"id"`
	Name      string      `gorm:"size:128" json:"name"`
	KeyHash   string      `gorm:"column:key_hash;size:64;uniqueIndex" json:"-"`
	Role      string      `gorm:"size:16" json:"role"`
	DeptID    *uint64     `gorm:"column:dept_id" json:"dept_id,omitempty"`
	Active    bool        `json:"active"`
	CreatedAt time.Time   `json:"created_at"`
	Dept      *Department `gorm:"-" json:"department,omitempty"`
}

func (APIKey) TableName() string { return "api_keys" }

type Event struct {
	ID          uint64    `gorm:"primaryKey" json:"id"`
	Source      string    `gorm:"size:64;default:api;uniqueIndex:uq_events_source_id,priority:1" json:"source"`
	EventID     string    `gorm:"column:event_id;size:128;uniqueIndex:uq_events_source_id,priority:2" json:"event_id"`
	EmployeeID  uint64    `gorm:"column:employee_id;index" json:"employee_id"`
	EventType   string    `gorm:"column:event_type;size:32" json:"event_type"`
	OccurredAt  time.Time `gorm:"column:occurred_at" json:"occurred_at"`
	Metadata    JSONMap   `gorm:"type:json" json:"metadata,omitempty"`
	ContentHash string    `gorm:"column:content_hash;size:64" json:"-"`
	IngestToken string    `gorm:"column:ingest_token;size:32" json:"-"`
	Processed   bool      `gorm:"index" json:"processed"`
	ReceivedAt  time.Time `json:"received_at"`
}

func (Event) TableName() string { return "events" }

type Rule struct {
	ID          uint64    `gorm:"primaryKey" json:"id"`
	Code        string    `gorm:"size:32;uniqueIndex:uq_rules_active_code,priority:2" json:"code"`
	Name        string    `gorm:"size:128" json:"name"`
	Description string    `gorm:"size:512" json:"description"`
	Enabled     bool      `json:"enabled"`
	Params      JSONMap   `gorm:"type:json" json:"params"`
	Version     uint32    `json:"version"`
	Active      bool      `gorm:"uniqueIndex:uq_rules_active_code,priority:1" json:"active"`
	CreatedAt   time.Time `json:"created_at"`
	UpdatedAt   time.Time `json:"updated_at"`
}

func (Rule) TableName() string { return "rules" }

type Alert struct {
	ID             uint64     `gorm:"primaryKey" json:"id"`
	RuleCode       string     `gorm:"size:32" json:"rule_code"`
	RuleVersion    uint32     `json:"rule_version"`
	EmployeeID     uint64     `gorm:"column:employee_id;index" json:"employee_id"`
	Status         string     `gorm:"size:32;index" json:"status"`
	Severity       string     `gorm:"size:16" json:"severity"`
	Title          string     `gorm:"size:255" json:"title"`
	DedupKey       string     `gorm:"column:dedup_key;size:190;uniqueIndex" json:"dedup_key"`
	WindowStart    *time.Time `gorm:"column:window_start" json:"window_start,omitempty"`
	WindowEnd      *time.Time `gorm:"column:window_end" json:"window_end,omitempty"`
	Evidence       JSONMap    `gorm:"type:json" json:"evidence"`
	FiredAt        time.Time  `json:"fired_at"`
	AcknowledgedAt *time.Time `gorm:"column:acknowledged_at" json:"acknowledged_at,omitempty"`
	EscalatedAt    *time.Time `gorm:"column:escalated_at" json:"escalated_at,omitempty"`
	ResolvedAt     *time.Time `gorm:"column:resolved_at" json:"resolved_at,omitempty"`
	LastEventAt    *time.Time `gorm:"column:last_event_at" json:"last_event_at,omitempty"`
	UpdatedAt      time.Time  `json:"updated_at"`
}

func (Alert) TableName() string { return "alerts" }

type WindowEvaluation struct {
	ID          uint64    `gorm:"primaryKey"`
	RuleCode    string    `gorm:"size:32;uniqueIndex:uq_window_eval,priority:1"`
	RuleVersion uint32    `gorm:"uniqueIndex:uq_window_eval,priority:2"`
	EmployeeID  uint64    `gorm:"uniqueIndex:uq_window_eval,priority:3"`
	WindowScope string    `gorm:"size:16;uniqueIndex:uq_window_eval,priority:4"`
	WindowDate  time.Time `gorm:"type:date;uniqueIndex:uq_window_eval,priority:5"`
	WindowStart time.Time `gorm:"column:window_start"`
	WindowEnd   time.Time
	EvaluatedAt time.Time
	HadAlert    bool
}

func (WindowEvaluation) TableName() string { return "window_evaluations" }

type AuditLog struct {
	ID         uint64    `gorm:"primaryKey" json:"id"`
	ActorKeyID *uint64   `gorm:"column:actor_key_id" json:"actor_key_id,omitempty"`
	ActorName  string    `gorm:"size:128" json:"actor_name"`
	Action     string    `gorm:"size:64;index" json:"action"`
	EntityType string    `gorm:"size:32" json:"entity_type"`
	EntityID   string    `gorm:"size:64" json:"entity_id"`
	Detail     JSONMap   `gorm:"type:json" json:"detail,omitempty"`
	CreatedAt  time.Time `json:"created_at"`
}

func (AuditLog) TableName() string { return "audit_logs" }

var _ sql.Scanner = (*JSONMap)(nil)
