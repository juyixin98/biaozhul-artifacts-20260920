package repo

import (
	"time"
)

type Department struct {
	ID     int64  `db:"id"`
	Name   string `db:"name"`
	Exempt bool   `db:"exempt"`
}

type Employee struct {
	ID           int64  `db:"id"`
	DepartmentID int64  `db:"department_id"`
	FullName     string `db:"full_name"`
	Timezone     string `db:"timezone"`
	Active       bool   `db:"active"`
}

type Workstation struct {
	ID         int64  `db:"id"`
	EmployeeID int64  `db:"employee_id"`
	Label      string `db:"label"`
}

// Policy is one published monitoring-policy version.
type Policy struct {
	Version           int      `db:"version"`
	WindowStartMinute int      `db:"window_start_minute"`
	WindowEndMinute   int      `db:"window_end_minute"`
	ExcludedPatterns  []string `db:"-"`
	ExemptDepartments []int64  `db:"-"`
}

type ClassificationRule struct {
	RuleID   int64  `db:"rule_id"`
	Pattern  string `db:"pattern"`
	Category string `db:"category"`
	Priority int    `db:"priority"`
}

type Classification struct {
	Version int
	Rules   []ClassificationRule
}

// SnapshotIn is one minute of activity as sent by an agent.
type SnapshotIn struct {
	WorkstationID int64     `json:"workstation_id"`
	EmployeeID    int64     `json:"employee_id"`
	MinuteUTC     time.Time `json:"minute_utc"`
	AppName       string    `json:"app_name"`
	ActivityCount int       `json:"activity_count"`
}

// RawSnapshot is a fully resolved, persisted snapshot.
type RawSnapshot struct {
	ID             int64     `db:"id"`
	WorkstationID  int64     `db:"workstation_id"`
	EmployeeID     int64     `db:"employee_id"`
	DepartmentID   int64     `db:"department_id"`
	MinuteUTC      time.Time `db:"minute_utc"`
	AppName        string    `db:"app_name"`
	ActivityCount  int       `db:"activity_count"`
	PolicyVersion  int       `db:"policy_version"`
	ClassifVersion int       `db:"classif_version"`
	Category       string    `db:"category"`
	LocalDate      time.Time `db:"local_date"`
	ReceivedAt     time.Time `db:"received_at"`
}

type DailySummary struct {
	EmployeeID         int64     `db:"employee_id" json:"employee_id"`
	LocalDate          time.Time `db:"local_date" json:"local_date"`
	DepartmentID       int64     `db:"department_id" json:"department_id"`
	ProductiveCount    int64     `db:"productive_count" json:"productive_count"`
	NonProductiveCount int64     `db:"non_productive_count" json:"non_productive_count"`
	NeutralCount       int64     `db:"neutral_count" json:"neutral_count"`
	ActiveMinutes      int       `db:"active_minutes" json:"active_minutes"`
	PolicyVersion      int       `db:"policy_version" json:"policy_version"`
	ClassifVersion     int       `db:"classif_version" json:"classif_version"`
	RecomputedAt       time.Time `db:"recomputed_at" json:"recomputed_at"`
}

type WeeklySummary struct {
	DepartmentID       int64     `db:"department_id" json:"department_id"`
	IsoWeek            time.Time `db:"iso_week" json:"iso_week"`
	ProductiveCount    int64     `db:"productive_count" json:"productive_count"`
	NonProductiveCount int64     `db:"non_productive_count" json:"non_productive_count"`
	NeutralCount       int64     `db:"neutral_count" json:"neutral_count"`
	ActiveEmployeeDays int       `db:"active_employee_days" json:"active_employee_days"`
	RecomputedAt       time.Time `db:"recomputed_at" json:"recomputed_at"`
}

type DayCoverage struct {
	EmployeeID int64      `db:"employee_id"`
	LocalDate  time.Time  `db:"local_date"`
	RawState   string     `db:"raw_state"`
	RawMinutes int        `db:"raw_minutes"`
	PurgedAt   *time.Time `db:"purged_at"`
}

type APIToken struct {
	Token        string `db:"token"`
	Role         string `db:"role"`
	DepartmentID *int64 `db:"department_id"`
	Description  string `db:"description"`
}
