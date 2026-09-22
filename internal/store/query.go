package store

import (
	"errors"

	"gorm.io/gorm"
)

// AssignedPoint is a point joined with its assignment under one catalog
// version. RegionID == 0 means unassigned.
type AssignedPoint struct {
	ID             uint64  `json:"id"`
	ExternalID     string  `json:"external_id"`
	Lat            float64 `json:"lat"`
	Lng            float64 `json:"lng"`
	Version        int     `json:"version"` // point optimistic version
	CatalogVersion int     `json:"catalog_version"`
	RegionID       uint64  `json:"region_id"`
	RegionVersion  int     `json:"region_version"`
	RegionName     string  `json:"region_name,omitempty"`
	OnBoundary     bool    `json:"on_boundary"`
}

const assignedSelect = `p.id AS id, p.external_id AS external_id, p.lat AS lat, p.lng AS lng,
	p.update_seq AS version, pa.catalog_version AS catalog_version,
	pa.region_id AS region_id, pa.region_version AS region_version,
	rv.name AS region_name, pa.on_boundary AS on_boundary`

func assignedQuery(gdb *gorm.DB, orgID uint64, catalogVersion int) *gorm.DB {
	return gdb.Table("points AS p").
		Select(assignedSelect).
		Joins(`JOIN point_assignments AS pa
			ON pa.point_id = p.id AND pa.org_id = p.org_id AND pa.catalog_version = ?`, catalogVersion).
		Joins(`LEFT JOIN region_versions AS rv ON rv.id = pa.region_version_id`).
		Where("p.org_id = ?", orgID)
}

// GetPointByExternalID returns a point plus its assignment under a catalog
// version.
func GetPointByExternalID(gdb *gorm.DB, orgID uint64, externalID string, catalogVersion int) (*AssignedPoint, error) {
	var ap AssignedPoint
	err := assignedQuery(gdb, orgID, catalogVersion).
		Where("p.external_id = ?", externalID).Take(&ap).Error
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, err
	}
	return &ap, nil
}

// BBoxFilter narrows a bounding-box query.
type BBoxFilter struct {
	MinLat, MaxLat, MinLng, MaxLng float64
	AssignedOnly                   bool
	Limit                          int
	Offset                         int
}

// BBoxQuery lists points within a latitude/longitude bounding box for one
// organization, joined with assignments under catalogVersion.
//
// The box may NOT cross the antimeridian; minLng must be <= maxLng. That case
// is rejected at the API boundary rather than silently producing a wrong set.
func BBoxQuery(gdb *gorm.DB, orgID uint64, catalogVersion int, f BBoxFilter) ([]AssignedPoint, error) {
	q := assignedQuery(gdb, orgID, catalogVersion).
		Where("p.lat BETWEEN ? AND ?", f.MinLat, f.MaxLat).
		Where("p.lng BETWEEN ? AND ?", f.MinLng, f.MaxLng)
	if f.AssignedOnly {
		q = q.Where("pa.region_id > 0")
	}
	var out []AssignedPoint
	err := q.Order("p.id ASC").Limit(f.Limit).Offset(f.Offset).Scan(&out).Error
	if out == nil {
		out = []AssignedPoint{}
	}
	return out, err
}

// NearestRow is a nearest-query hit with its Haversine distance in meters.
type NearestRow struct {
	AssignedPoint
	DistanceM float64 `json:"distance_m"`
}

// NearestQuery returns up to n points nearest (lat,lng) by great-circle
// distance. The distance is computed in SQL with the Haversine formula; ties
// on distance are broken by point ID ascending so ordering is deterministic.
//
// Every row is constrained by org_id in both the points and the assignment
// join: a point belonging to another organization can never be returned.
func NearestQuery(gdb *gorm.DB, orgID uint64, catalogVersion int,
	lat, lng float64, n int, assignedOnly bool) ([]NearestRow, error) {

	// Haversine in SQL. The two constants (? parameters) are the reference
	// latitude (used twice) and longitude; coordinates are validated upstream.
	const toRad = "* 0.017453292519943295"
	hav := `6371000 * 2 * ASIN(SQRT(
		POWER(SIN(((? ` + toRad + `) - (p.lat ` + toRad + `)) / 2), 2) +
		COS(? ` + toRad + `) * COS(p.lat ` + toRad + `) *
		POWER(SIN(((? ` + toRad + `) - (p.lng ` + toRad + `)) / 2), 2)
	))`

	q := gdb.Table("points AS p").
		Select(assignedSelect+", "+hav+" AS distance_m", lat, lat, lng).
		Joins(`JOIN point_assignments AS pa
			ON pa.point_id = p.id AND pa.org_id = p.org_id AND pa.catalog_version = ?`, catalogVersion).
		Joins(`LEFT JOIN region_versions AS rv ON rv.id = pa.region_version_id`).
		Where("p.org_id = ?", orgID)
	if assignedOnly {
		q = q.Where("pa.region_id > 0")
	}
	var out []NearestRow
	err := q.Order("distance_m ASC, p.id ASC").Limit(n).Scan(&out).Error
	if out == nil {
		out = []NearestRow{}
	}
	return out, err
}
