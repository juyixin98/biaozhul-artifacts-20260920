// Package models defines the GORM-backed data model for GeoTerritory.
//
// The schema is created from migrations/ SQL (the source of truth); the
// struct tags here mirror it so GORM can build queries. AutoMigrate is not
// used in production.
package models

import (
	"database/sql/driver"
	"encoding/json"
	"errors"
	"time"

	"geoterritory/geometry"
)

// Vertices is the JSON column type holding a polygon ring (no repeated
// closing vertex).
type Vertices []geometry.LatLng

// Value implements driver.Valuer.
func (v Vertices) Value() (driver.Value, error) {
	return json.Marshal([]geometry.LatLng(v))
}

// Scan implements sql.Scanner.
func (v *Vertices) Scan(src any) error {
	if src == nil {
		*v = nil
		return nil
	}
	var b []byte
	switch t := src.(type) {
	case []byte:
		b = t
	case string:
		b = []byte(t)
	default:
		return errors.New("vertices: unsupported scan source")
	}
	return json.Unmarshal(b, (*[]geometry.LatLng)(v))
}

// GormDataType tells the MySQL driver the column is JSON.
func (Vertices) GormDataType() string { return "json" }

// Organization is the authorization scope. Requests authenticate with an
// organization-scoped API key and can only ever see that org's data.
type Organization struct {
	ID        uint64    `gorm:"primaryKey;autoIncrement" json:"id"`
	Name      string    `gorm:"size:128;not null" json:"name"`
	APIKey    string    `gorm:"size:128;not null;uniqueIndex:uniq_org_api_key" json:"api_key,omitempty"`
	CreatedAt time.Time `gorm:"not null" json:"created_at"`
}

// Region is a territory definition. Each publish creates an immutable
// RegionVersion; the region row carries only identity and the current name.
type Region struct {
	ID            uint64    `gorm:"primaryKey;autoIncrement" json:"id"`
	OrgID         uint64    `gorm:"not null;uniqueIndex:uniq_region_org_name,priority:1;index:idx_region_org" json:"org_id"`
	Name          string    `gorm:"size:128;not null;uniqueIndex:uniq_region_org_name,priority:2" json:"name"`
	LatestVersion int       `gorm:"not null;default:0" json:"latest_version"`
	Priority      int       `gorm:"not null" json:"priority"`
	Active        bool      `gorm:"not null;default:true" json:"active"`
	CreatedAt     time.Time `gorm:"not null" json:"created_at"`
	UpdatedAt     time.Time `gorm:"not null" json:"updated_at"`
}

// RegionVersion is an immutable snapshot of one region's polygon at one
// point in time. (region_id, version) is unique; the same polygon snapshot
// is referenced from immutable catalogs.
type RegionVersion struct {
	ID        uint64    `gorm:"primaryKey;autoIncrement" json:"-"`
	RegionID  uint64    `gorm:"not null;uniqueIndex:uniq_region_version,priority:1" json:"region_id"`
	Version   int       `gorm:"not null;uniqueIndex:uniq_region_version,priority:2" json:"version"`
	OrgID     uint64    `gorm:"not null;index:idx_rv_org_ver,priority:1" json:"org_id"`
	Name      string    `gorm:"size:128;not null" json:"name"`
	Priority  int       `gorm:"not null" json:"priority"`
	Vertices  Vertices  `gorm:"type:json;not null" json:"vertices"`
	MinLat    float64   `gorm:"not null" json:"-"`
	MinLng    float64   `gorm:"not null" json:"-"`
	MaxLat    float64   `gorm:"not null" json:"-"`
	MaxLng    float64   `gorm:"not null" json:"-"`
	AreaSqDeg float64   `gorm:"not null" json:"area_sq_deg"`
	CreatedAt time.Time `gorm:"not null" json:"created_at"`
}

// CatalogState holds the per-org catalog pointers:
//   - CurrentVersion is the version assignments and queries are based on;
//   - PublishedVersion is set at publish time and moves to Current only after
//     the full reassignment has completed atomically.
//
// Version 0 is the seeded empty catalog (no regions).
type CatalogState struct {
	OrgID            uint64    `gorm:"primaryKey" json:"org_id"`
	CurrentVersion   int       `gorm:"not null;default:0" json:"current_version"`
	PublishedVersion int       `gorm:"not null;default:0" json:"published_version"`
	UpdatedAt        time.Time `gorm:"not null" json:"updated_at"`
}

// TableName pins the table name (GORM would snake_case the type as
// catalog_states otherwise).
func (CatalogState) TableName() string { return "region_catalog_state" }

// CatalogEntry is the immutable membership list of a catalog version.
type CatalogEntry struct {
	ID              uint64    `gorm:"primaryKey;autoIncrement" json:"-"`
	OrgID           uint64    `gorm:"not null;uniqueIndex:uniq_catalog_entry,priority:1;index:idx_ce_org_ver,priority:1" json:"org_id"`
	CatalogVersion  int       `gorm:"not null;uniqueIndex:uniq_catalog_entry,priority:2;index:idx_ce_org_ver,priority:2" json:"catalog_version"`
	RegionVersionID uint64    `gorm:"not null;uniqueIndex:uniq_catalog_entry,priority:3" json:"region_version_id"`
	RegionID        uint64    `gorm:"not null;index:idx_ce_region" json:"region_id"`
	Version         int       `gorm:"not null" json:"version"`
	CreatedAt       time.Time `gorm:"not null" json:"created_at"`
}

// ReassignJob is the durable, crash-recoverable reassignment task created by
// a region publish.
type ReassignJob struct {
	ID              uint64     `gorm:"primaryKey;autoIncrement" json:"id"`
	OrgID           uint64     `gorm:"not null" json:"org_id"`
	FromVersion     int        `gorm:"not null" json:"from_version"`
	ToVersion       int        `gorm:"not null" json:"to_version"`
	Status          string     `gorm:"not null;index:idx_job_status" json:"status"` // PENDING/RUNNING/DONE/FAILED/CANCELLED
	TotalPoints     int64      `gorm:"not null;default:0" json:"total_points"`
	ProcessedPoints int64      `gorm:"not null;default:0" json:"processed_points"`
	Error           string     `gorm:"type:text" json:"error,omitempty"`
	CreatedAt       time.Time  `gorm:"not null" json:"created_at"`
	UpdatedAt       time.Time  `gorm:"not null" json:"updated_at"`
	HeartbeatAt     *time.Time `json:"heartbeat_at,omitempty"`
	CompletedAt     *time.Time `json:"completed_at,omitempty"`
}

// TableName pins the physical table name.
func (ReassignJob) TableName() string { return "reassignment_jobs" }

// Job status values.
const (
	JobPending   = "PENDING"
	JobRunning   = "RUNNING"
	JobDone      = "DONE"
	JobFailed    = "FAILED"
	JobCancelled = "CANCELLED"
)

// Point is an externally imported location. Assignment is NOT stored here; it
// lives in PointAssignment per catalog version, so reads against old versions
// stay consistent after a republish.
type Point struct {
	ID         uint64  `gorm:"primaryKey;autoIncrement" json:"id"`
	OrgID      uint64  `gorm:"not null;uniqueIndex:uniq_point_org_ext,priority:1;index:idx_point_org_geo,priority:1" json:"org_id"`
	ExternalID string  `gorm:"size:128;not null;uniqueIndex:uniq_point_org_ext,priority:2" json:"external_id"`
	Lat        float64 `gorm:"not null;index:idx_point_org_geo,priority:2" json:"lat"`
	Lng        float64 `gorm:"not null;index:idx_point_org_geo,priority:3" json:"lng"`
	// UpdateSeq is the optimistic-concurrency version. External writers pass
	// the seq of the row they last saw; concurrent updates can never silently
	// overwrite each other.
	UpdateSeq int       `gorm:"not null;default:1" json:"version"`
	CreatedAt time.Time `gorm:"not null" json:"created_at"`
	UpdatedAt time.Time `gorm:"not null" json:"updated_at"`
}

// PointAssignment records which region a point belongs to under one catalog
// version. Empty RegionID (value 0) means "outside every region / unassigned".
// The row is written whenever the point is created or moved, for every live
// catalog version (current and a not-yet-flipped published version).
type PointAssignment struct {
	PointID         uint64    `gorm:"primaryKey" json:"point_id"`
	OrgID           uint64    `gorm:"not null;uniqueIndex:uniq_pa_org_ver_point,priority:1;index:idx_pa_org_ver_region,priority:1" json:"org_id"`
	CatalogVersion  int       `gorm:"not null;uniqueIndex:uniq_pa_org_ver_point,priority:2;index:idx_pa_org_ver_region,priority:2" json:"catalog_version"`
	RegionID        uint64    `gorm:"not null;default:0;index:idx_pa_org_ver_region,priority:3" json:"region_id"`
	RegionVersionID uint64    `gorm:"not null;default:0" json:"region_version_id"`
	RegionVersion   int       `gorm:"not null;default:0" json:"region_version"`
	OnBoundary      bool      `gorm:"not null;default:false" json:"on_boundary"`
	UpdatedAt       time.Time `gorm:"not null" json:"updated_at"`
}
