package store

import (
	"errors"
	"time"

	"gorm.io/gorm"

	"geoterritory/geometry"
	"geoterritory/internal/engine"
	"geoterritory/internal/models"
)

// Per-row error codes for batch import.
const (
	RowOK            = "OK"
	RowErrValidation = "VALIDATION_ERROR"
	RowErrVersion    = "VERSION_CONFLICT"
	RowErrInternal   = "INTERNAL_ERROR"
)

// ErrVersionConflict is the optimistic-concurrency failure: the caller
// supplied the seq of an older revision of this point.
var ErrVersionConflict = errors.New("point was modified concurrently; refetch its version and retry")

// PointRow is one import row.
type PointRow struct {
	ExternalID string
	Lat        float64
	Lng        float64
	// ExpectedVersion is the update_seq the caller last saw.
	//   nil  -> no expectation (pure insert or last-write-write on change);
	//   0    -> point must not exist yet (insert only);
	//   N>0  -> point must exist at exactly seq N (optimistic update).
	ExpectedVersion *int
}

// PointResult is the per-row outcome of a batch import.
type PointResult struct {
	Index      int    `json:"index"`
	ExternalID string `json:"external_id"`
	Status     string `json:"status"`
	Error      string `json:"error,omitempty"`
	ID         uint64 `json:"id,omitempty"`
	Version    int    `json:"version,omitempty"`
	Created    bool   `json:"created"`
	Updated    bool   `json:"updated"`
}

// BatchUpsertPoints imports up to 1000 rows in the single lock-protected
//
// The caller must hold the org lock; tx is that transaction. currentCat and
// publishedCat are the two catalog views to write assignments for.
func BatchUpsertPoints(tx *gorm.DB, orgID uint64, rows []PointRow,
	currentCat, publishedCat *engine.Catalog) ([]PointResult, error) {

	results := make([]PointResult, len(rows))
	now := time.Now().UTC()

	for i, row := range rows {
		res := PointResult{Index: i, ExternalID: row.ExternalID}
		err := processPointRow(tx, orgID, row, now, currentCat, publishedCat, &res)
		if err != nil {
			res.Status = RowErrInternal
			res.Error = err.Error()
		}
		if res.Status == "" {
			res.Status = RowOK
		}
		results[i] = res
	}
	return results, nil
}

func processPointRow(tx *gorm.DB, orgID uint64, row PointRow, now time.Time,
	currentCat, publishedCat *engine.Catalog, res *PointResult) error {

	var p models.Point
	err := tx.Where("org_id = ? AND external_id = ?", orgID, row.ExternalID).First(&p).Error
	found := true
	if errors.Is(err, gorm.ErrRecordNotFound) {
		found = false
	} else if err != nil {
		return err
	}

	if found {
		moved := p.Lat != row.Lat || p.Lng != row.Lng
		if moved {
			// A coordinate change is an optimistic update: an expected version
			// is mandatory, and it must equal the current seq. Two concurrent
			// movers can never silently overwrite each other.
			if row.ExpectedVersion == nil {
				res.Status = RowErrVersion
				res.Error = "coordinates changed: expected_version is required"
				res.Version = p.UpdateSeq
				return nil
			}
			if *row.ExpectedVersion != p.UpdateSeq {
				res.Status = RowErrVersion
				res.Error = ErrVersionConflict.Error()
				res.Version = p.UpdateSeq
				return nil
			}
			p.Lat = row.Lat
			p.Lng = row.Lng
			p.UpdateSeq++
			p.UpdatedAt = now
			if err := tx.Save(&p).Error; err != nil {
				return err
			}
			res.Updated = true
		}
		// (Re)write assignment rows for every live version. On an idempotent
		// same-coordinate write this is also the repair path that fills a
		// version the worker may not have reached yet. UPSERT (not IGNORE):
		// this writer holds the newest coordinate.
		if err := upsertAssignment(tx, orgID, p.ID, now, currentCat, p.Lat, p.Lng); err != nil {
			return err
		}
		if publishedCat.Version != currentCat.Version {
			if err := upsertAssignment(tx, orgID, p.ID, now, publishedCat, p.Lat, p.Lng); err != nil {
				return err
			}
		}
		res.ID = p.ID
		res.Version = p.UpdateSeq
		return nil
	}

	// Insert: expected version 0 is implicit for a missing point.
	if row.ExpectedVersion != nil && *row.ExpectedVersion != 0 {
		res.Status = RowErrVersion
		res.Error = "point does not exist but expected version > 0"
		return nil
	}
	p = models.Point{
		OrgID:      orgID,
		ExternalID: row.ExternalID,
		Lat:        row.Lat,
		Lng:        row.Lng,
		UpdateSeq:  1,
		CreatedAt:  now,
		UpdatedAt:  now,
	}
	if err := tx.Create(&p).Error; err != nil {
		return err
	}
	res.Created = true
	if err := upsertAssignment(tx, orgID, p.ID, now, currentCat, p.Lat, p.Lng); err != nil {
		return err
	}
	if publishedCat.Version != currentCat.Version {
		if err := upsertAssignment(tx, orgID, p.ID, now, publishedCat, p.Lat, p.Lng); err != nil {
			return err
		}
	}
	res.ID = p.ID
	res.Version = 1
	return nil
}

func upsertAssignment(tx *gorm.DB, orgID, pointID uint64, now time.Time,
	cat *engine.Catalog, lat, lng float64) error {
	d := cat.AssignPoint(geometry.LatLng{Lat: lat, Lng: lng})
	pa := models.PointAssignment{
		PointID:         pointID,
		OrgID:           orgID,
		CatalogVersion:  cat.Version,
		RegionID:        d.RegionID,
		RegionVersionID: d.RegionVersionID,
		RegionVersion:   d.RegionVersion,
		OnBoundary:      d.OnBoundary,
		UpdatedAt:       now,
	}
	// ON DUPLICATE KEY UPDATE: writer path always wins over a stale worker
	// row for the same point/version, because the writer holds the newest
	// coordinate.
	return tx.Exec(`INSERT INTO point_assignments
		(point_id, org_id, catalog_version, region_id, region_version_id, region_version, on_boundary, updated_at)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?)
		ON DUPLICATE KEY UPDATE
			region_id = VALUES(region_id),
			region_version_id = VALUES(region_version_id),
			region_version = VALUES(region_version),
			on_boundary = VALUES(on_boundary),
			updated_at = VALUES(updated_at)`,
		pa.PointID, pa.OrgID, pa.CatalogVersion, pa.RegionID, pa.RegionVersionID,
		pa.RegionVersion, pa.OnBoundary, pa.UpdatedAt).Error
}
