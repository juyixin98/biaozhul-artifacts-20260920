// Package model holds the shared domain types.
package model

import "time"

// SnapshotInput is one per-minute activity snapshot as delivered by a
// workstation agent. No raw keystrokes ever leave the workstation — only
// an aggregate activity count per application per minute.
type SnapshotInput struct {
	WorkstationID string    `json:"workstation_id"`
	EmployeeID    int64     `json:"employee_id"`
	CapturedAt    time.Time `json:"captured_at"`
	AppName       string    `json:"app_name"`
	ActivityCount int64     `json:"activity_count"`
}

// IngestResult summarises what a batch did.
type IngestResult struct {
	Received   int `json:"received"`
	Inserted   int `json:"inserted"`
	Duplicates int `json:"duplicates"` // identical re-deliveries, processed once
	Filtered   int `json:"filtered"`   // dropped by privacy policy, never persisted
}

type Employee struct {
	ID           int64  `db:"id" json:"id"`
	DepartmentID int64  `db:"department_id" json:"department_id"`
	Name         string `db:"name" json:"name"`
	Timezone     string `db:"timezone" json:"timezone"`
}

type User struct {
	ID           int64  `db:"id"`
	Username     string `db:"username"`
	Token        string `db:"token"`
	Role         string `db:"role"` // admin | manager | ingest
	DepartmentID *int64 `db:"department_id"`
}

type DailySummary struct {
	EmployeeID   int64     `db:"employee_id" json:"employee_id"`
	Day          time.Time `db:"day" json:"day"`
	Productive   int64     `db:"productive" json:"productive"`
	Unproductive int64     `db:"unproductive" json:"unproductive"`
	Neutral      int64     `db:"neutral" json:"neutral"`
	Total        int64     `db:"total" json:"total"`
}

type WeeklySummary struct {
	DepartmentID int64     `db:"department_id" json:"department_id"`
	WeekStart    time.Time `db:"week_start" json:"week_start"`
	Productive   int64     `db:"productive" json:"productive"`
	Unproductive int64     `db:"unproductive" json:"unproductive"`
	Neutral      int64     `db:"neutral" json:"neutral"`
	Total        int64     `db:"total" json:"total"`
}

type RawSnapshot struct {
	ID                    int64     `db:"id" json:"id"`
	WorkstationID         string    `db:"workstation_id" json:"workstation_id"`
	EmployeeID            int64     `db:"employee_id" json:"employee_id"`
	MinuteUTC             time.Time `db:"minute_utc" json:"minute_utc"`
	AppName               string    `db:"app_name" json:"app_name"`
	ActivityCount         int64     `db:"activity_count" json:"activity_count"`
	Category              string    `db:"category" json:"category"`
	PolicyVersion         int       `db:"policy_version" json:"policy_version"`
	ClassificationVersion int       `db:"classification_version" json:"classification_version"`
}
