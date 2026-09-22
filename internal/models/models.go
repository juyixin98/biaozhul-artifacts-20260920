// Package models holds the GORM-mapped persistence types. Schema is created
// from versioned SQL files in migrations/ (see store.Migrator); the struct
// tags here document column meaning and index intent.
package models

import "time"

// Organization is the tenant boundary. Every region and point belongs to an
// organization and cross-org visibility is never allowed.
type Organization struct {
	ID           int64     `gorm:"column:id;primaryKey;autoIncrement" json:"id"`
	Name         string    `gorm:"column:name;type:varchar(128);not null" json:"name"`
	APIKey       string    `gorm:"column:api_key;type:varchar(64);not null;uniqueIndex" json:"-"`
	ActiveSetSeq int64     `gorm:"column:active_set_seq;not null;default:0" json:"active_set_seq"`
	CreatedAt    time.Time `gorm:"column:created_at;not null" json:"created_at"`
}

func (Organization) TableName() string { return "organizations" }

// Region groups immutable versions of one named territory. Assignment rules
// always reference region_versions, never this row directly.
type Region struct {
	ID              int64     `gorm:"column:id;primaryKey;autoIncrement" json:"id"`
	OrgID           int64     `gorm:"column:org_id;not null;uniqueIndex:uk_org_name" json:"org_id"`
	Name            string    `gorm:"column:name;type:varchar(128);not null;uniqueIndex:uk_org_name" json:"name"`
	ActiveVersionID int64     `gorm:"column:active_version_id" json:"active_version_id"` // NULL until first publish completes
	CreatedAt       time.Time `gorm:"column:created_at;not null" json:"created_at"`
}

func (Region) TableName() string { return "regions" }

// RegionVersion is an immutable snapshot: polygon + priority + the set_seq
// under which it became effective. Once status leaves 'building' the polygon
// JSON never changes again; a change is a new row.
type RegionVersion struct {
	ID        int64     `gorm:"column:id;primaryKey;autoIncrement" json:"id"`
	RegionID  int64     `gorm:"column:region_id;not null;index" json:"region_id"`
	OrgID     int64     `gorm:"column:org_id;not null;index" json:"org_id"`
	Version   int       `gorm:"column:version;not null" json:"version"`           // per-region increment
	Priority  int       `gorm:"column:priority;not null" json:"priority"`         // 1 = highest ... 5 = lowest
	Polygon   string    `gorm:"column:polygon;type:json;not null" json:"polygon"` // [{"lng":..,"lat":..}, ...]
	SetSeq    int64     `gorm:"column:set_seq;not null;default:0" json:"set_seq"` // 0 while building
	Status    string    `gorm:"column:status;type:enum('building','active','superseded');not null" json:"status"`
	CreatedAt time.Time `gorm:"column:created_at;not null" json:"created_at"`
}

func (RegionVersion) TableName() string { return "region_versions" }

// Point is a tracked location. Version is the optimistic-concurrency token
// for coordinate updates; assign_set_seq records the region-version set the
// current assignment was computed against.
type Point struct {
	ID              int64     `gorm:"column:id;primaryKey;autoIncrement" json:"id"`
	OrgID           int64     `gorm:"column:org_id;not null;uniqueIndex:uk_org_ext" json:"org_id"`
	ExternalID      string    `gorm:"column:external_id;type:varchar(128);not null;uniqueIndex:uk_org_ext" json:"external_id"`
	Lng             float64   `gorm:"column:lng;type:double;not null" json:"lng"`
	Lat             float64   `gorm:"column:lat;type:double;not null" json:"lat"`
	Version         int64     `gorm:"column:version;not null;default:1" json:"version"`
	RegionID        *int64    `gorm:"column:region_id;index" json:"region_id"`           // nil = unassigned
	RegionVersionID *int64    `gorm:"column:region_version_id" json:"region_version_id"` // basis version, nil when unassigned
	AssignSetSeq    int64     `gorm:"column:assign_set_seq;not null;default:0" json:"assign_set_seq"`
	CreatedAt       time.Time `gorm:"column:created_at;not null" json:"created_at"`
	UpdatedAt       time.Time `gorm:"column:updated_at;not null" json:"updated_at"`
}

func (Point) TableName() string { return "points" }

// ReassignJob is the persistent record of one recomputation. Rows survive
// crashes; the worker adopts jobs left in 'pending'/'running' at startup.
type ReassignJob struct {
	ID           int64      `gorm:"column:id;primaryKey;autoIncrement" json:"id"`
	OrgID        int64      `gorm:"column:org_id;not null;index" json:"org_id"`
	TargetSeq    int64      `gorm:"column:target_seq;not null" json:"target_seq"`
	NewVersionID int64      `gorm:"column:new_version_id;not null" json:"new_version_id"`
	Status       string     `gorm:"column:status;type:enum('pending','running','done','superseded','failed');not null;index" json:"status"`
	Error        string     `gorm:"column:error;type:varchar(1024);not null;default:''" json:"error"`
	CreatedAt    time.Time  `gorm:"column:created_at;not null" json:"created_at"`
	StartedAt    *time.Time `gorm:"column:started_at" json:"started_at"`
	FinishedAt   *time.Time `gorm:"column:finished_at" json:"finished_at"`
}

func (ReassignJob) TableName() string { return "reassign_jobs" }

// AssignmentStaging holds per-point recomputation results until the whole
// set is ready, at which point they are applied in a single transaction.
// Row version guards stale application: if the point moved after staging,
// the apply phase recomputes it instead.
type AssignmentStaging struct {
	JobID           int64  `gorm:"column:job_id;primaryKey"`
	PointID         int64  `gorm:"column:point_id;primaryKey;autoIncrement:false"`
	OrgID           int64  `gorm:"column:org_id;not null"`
	RegionID        *int64 `gorm:"column:region_id"`
	RegionVersionID *int64 `gorm:"column:region_version_id"`
	PointVersion    int64  `gorm:"column:point_version;not null"`
}

func (AssignmentStaging) TableName() string { return "assignment_staging" }
