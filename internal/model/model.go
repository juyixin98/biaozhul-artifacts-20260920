// Package model contains the shared domain types of the DeskLens backend.
package model

import (
	"time"
)

// Category values used for classified snapshots.
const (
	CategoryProductive   = "productive"
	CategoryUnproductive = "unproductive"
	CategoryNeutral      = "neutral"
)

// Filtered snapshot reasons reported back to the agent. Filtered rows are not
// stored anywhere.
const (
	ReasonExemptDepartment = "exempt_department"
	ReasonExcludedApp      = "excluded_app"
	ReasonOutsideWindow    = "outside_window"
)

type Department struct {
	ID        int64     `db:"id" json:"id"`
	Name      string    `db:"name" json:"name"`
	CreatedAt time.Time `db:"created_at" json:"created_at"`
}

type Employee struct {
	ID           int64     `db:"id" json:"id"`
	DepartmentID int64     `db:"department_id" json:"department_id"`
	DisplayName  string    `db:"display_name" json:"display_name"`
	Timezone     string    `db:"timezone" json:"timezone"`
	CreatedAt    time.Time `db:"created_at" json:"created_at"`
}

type Workstation struct {
	ID         string    `db:"id" json:"id"`
	EmployeeID int64     `db:"employee_id" json:"employee_id"`
	CreatedAt  time.Time `db:"created_at" json:"created_at"`
}

// Policy is the resolved, currently-published monitoring policy.
type Policy struct {
	Version            int32
	StartMinute        int
	EndMinute          int
	ExcludedPatterns   []string
	ExemptDepartmentID map[int64]bool
}

// Rule is one ordered classification rule.
type Rule struct {
	RuleID   string `db:"rule_id"`
	Pattern  string `db:"pattern"`
	Category string `db:"category"`
	Priority int    `db:"priority"`
}

// Classification is a full, versioned rule set.
type Classification struct {
	Version int64  `db:"version"`
	Rules   []Rule // ordered by priority ASC, rule_id ASC
}

// Snapshot is one validated per-minute activity sample, as accepted from an
// agent.
type Snapshot struct {
	WorkstationID string    `json:"workstation_id" validate:"required"`
	EmployeeID    int64     `json:"employee_id" validate:"required"`
	UTCTime       time.Time `json:"utc_time" validate:"required"`
	AppName       string    `json:"app_name" validate:"required"`
	ActivityCount int       `json:"activity_count"`
}
