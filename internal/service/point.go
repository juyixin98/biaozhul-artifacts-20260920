package service

import (
	"errors"
	"fmt"
	"math"
	"net/http"
	"sort"

	"geoterritory/internal/geometry"
	"geoterritory/internal/models"

	"gorm.io/gorm"
)

// MaxBatchSize bounds a single import request.
const MaxBatchSize = 1000

// MaxNearestN bounds the nearest-neighbour limit.
const MaxNearestN = 50

// PointService handles point ingestion (idempotent, optimistically locked)
// and org-scoped spatial reads.
type PointService struct {
	db *gorm.DB
}

func NewPointService(db *gorm.DB) *PointService { return &PointService{db: db} }

// BatchItem is one import row. ExpectedVersion is the optimistic token:
//   - omitted on the first occurrence: create allowed;
//   - when the row already exists with DIFFERENT coordinates it MUST match
//     the stored version, otherwise the row is rejected.
type BatchItem struct {
	ExternalID      string  `json:"external_id"`
	Lat             float64 `json:"lat"`
	Lng             float64 `json:"lng"`
	ExpectedVersion *int64  `json:"expected_version"`
}

// RowResult is the per-row outcome. Successful rows carry the resulting point
// so a partial failure still returns everything that was written.
type RowResult struct {
	ExternalID string        `json:"external_id"`
	Status     string        `json:"status"` // created | updated | unchanged | error
	Version    int64         `json:"version"`
	Point      *models.Point `json:"point,omitempty"`
	ErrorCode  string        `json:"error_code,omitempty"`
	Error      string        `json:"error,omitempty"`
}

// BatchImport upserts up to MaxBatchSize rows idempotently. All successful
// rows commit together; rejected rows return per-row errors.
func (s *PointService) BatchImport(orgID int64, items []BatchItem) ([]RowResult, error) {
	if len(items) == 0 {
		return nil, badRequest("batch must contain at least one point")
	}
	if len(items) > MaxBatchSize {
		return nil, badRequest("batch size %d exceeds maximum of %d", len(items), MaxBatchSize)
	}

	results := make([]RowResult, len(items))

	_ = s.db.Transaction(func(tx *gorm.DB) error {
		var org models.Organization
		if err := tx.Clauses(lockOrgClause()).First(&org, orgID).Error; err != nil {
			return err
		}
		// Compute assignment against the set effective RIGHT NOW. Building
		// versions are excluded, so a recompute in flight never leaks.
		set, err := loadEffectiveSet(tx, orgID)
		if err != nil {
			return err
		}

		seen := make(map[string]int, len(items))
		for i, item := range items {
			res := &results[i]
			res.ExternalID = item.ExternalID

			if item.ExternalID == "" {
				rowError(res, "EXTERNAL_ID_REQUIRED", "external_id is required")
				continue
			}
			if prev, dup := seen[item.ExternalID]; dup {
				rowError(res, "DUPLICATE_IN_BATCH",
					fmt.Sprintf("external_id appears more than once in this batch (first at row %d)", prev))
				continue
			}
			seen[item.ExternalID] = i

			if err := validateCoordinate(item.Lat, item.Lng); err != nil {
				rowError(res, "INVALID_COORDINATES", err.Error())
				continue
			}

			sp := fmt.Sprintf("sp%d", i)
			tx.SavePoint(sp)
			status, err := upsertPoint(tx, orgID, org.ActiveSetSeq, set, item)
			if err != nil {
				tx.RollbackTo(sp)
				var ve *ValidationError
				if errors.As(err, &ve) {
					rowError(res, "VERSION_CONFLICT", ve.Message)
				} else {
					rowError(res, "INTERNAL", err.Error())
				}
				continue
			}

			var p models.Point
			if err := tx.Where("org_id = ? AND external_id = ?", orgID, item.ExternalID).First(&p).Error; err != nil {
				tx.RollbackTo(sp)
				rowError(res, "INTERNAL", err.Error())
				continue
			}
			res.Status = status
			res.Version = p.Version
			res.Point = &p
		}
		return nil // commit: per-row failures are reported, never abort the batch
	})

	return results, nil
}

// UpdatePoint changes one point's coordinates. The optimistic token is
// mandatory: the update matches `WHERE version = expected` so two concurrent
// writers cannot silently overwrite each other.
func (s *PointService) UpdatePoint(orgID int64, externalID string, lat, lng float64, expected int64) (*models.Point, string, error) {
	if err := validateCoordinate(lat, lng); err != nil {
		return nil, "", err
	}
	var out models.Point
	status := "updated"
	err := s.db.Transaction(func(tx *gorm.DB) error {
		var org models.Organization
		if err := tx.Clauses(lockOrgClause()).First(&org, orgID).Error; err != nil {
			return err
		}
		set, err := loadEffectiveSet(tx, orgID)
		if err != nil {
			return err
		}
		var p models.Point
		if err := tx.Where("org_id = ? AND external_id = ?", orgID, externalID).First(&p).Error; err != nil {
			if errors.Is(err, gorm.ErrRecordNotFound) {
				return &ValidationError{Status: http.StatusNotFound, Message: "point not found"}
			}
			return err
		}
		if coordsEqual(p.Lat, lat, p.Lng, lng) {
			out = p
			status = "unchanged"
			return nil
		}
		if p.Version != expected {
			return &ValidationError{Status: http.StatusConflict,
				Message: fmt.Sprintf("version conflict: expected %d but point is at version %d; refetch and retry", expected, p.Version)}
		}
		assign := set.Assign(geometry.Vertex{Lng: lng, Lat: lat})
		res := tx.Model(&p).Where("version = ?", expected).Updates(map[string]any{
			"lat":               lat,
			"lng":               lng,
			"version":           expected + 1,
			"region_id":         assign.RegionID,
			"region_version_id": assign.RegionVersionID,
			"assign_set_seq":    org.ActiveSetSeq,
		})
		if res.Error != nil {
			return res.Error
		}
		if res.RowsAffected == 0 {
			return &ValidationError{Status: http.StatusConflict, Message: "version conflict: point changed concurrently; refetch and retry"}
		}
		out = p
		out.Lat = lat
		out.Lng = lng
		out.Version = expected + 1
		out.RegionID = assign.RegionID
		out.RegionVersionID = assign.RegionVersionID
		out.AssignSetSeq = org.ActiveSetSeq
		return nil
	})
	if err != nil {
		return nil, "", err
	}
	return &out, status, nil
}

// GetPoint fetches one point, scoped to the organization.
func (s *PointService) GetPoint(orgID int64, externalID string) (*models.Point, error) {
	var p models.Point
	if err := s.db.Where("org_id = ? AND external_id = ?", orgID, externalID).First(&p).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, &ValidationError{Status: http.StatusNotFound, Message: "point not found"}
		}
		return nil, err
	}
	return &p, nil
}

// BBoxResult is a bounding-box page.
type BBoxResult struct {
	Count  int            `json:"count"`
	Points []models.Point `json:"points"`
}

// BBox returns points inside the axis-aligned box. Boxes crossing the
// antimeridian (min_lng > max_lng) are REJECTED with 400 rather than wrapped.
func (s *PointService) BBox(orgID int64, b geometry.BBox, limit int) (*BBoxResult, error) {
	if err := validateBBox(b); err != nil {
		return nil, err
	}
	if limit <= 0 || limit > 1000 {
		limit = 1000
	}
	var points []models.Point
	err := s.db.Where("org_id = ? AND lat BETWEEN ? AND ? AND lng BETWEEN ? AND ?",
		orgID, b.MinLat, b.MaxLat, b.MinLng, b.MaxLng).
		Order("id ASC").Limit(limit).Find(&points).Error
	if err != nil {
		return nil, err
	}
	return &BBoxResult{Count: len(points), Points: points}, nil
}

// NearestPoint carries a point and its Haversine distance from the query.
type NearestPoint struct {
	models.Point
	DistanceMeters float64 `json:"distance_meters"`
}

// Nearest returns up to n closest points by great-circle distance. Ties at
// equal distance are broken by ascending point ID, so the result is fully
// deterministic. Every query filters org_id in the database; points of other
// organizations can never enter the candidate set.
func (s *PointService) Nearest(orgID int64, q geometry.Vertex, n int) ([]NearestPoint, error) {
	if err := validateCoordinate(q.Lat, q.Lng); err != nil {
		return nil, err
	}
	if n <= 0 {
		n = 10
	}
	if n > MaxNearestN {
		return nil, badRequest("n must be at most %d", MaxNearestN)
	}

	var points []models.Point
	if err := s.db.Where("org_id = ?", orgID).Find(&points).Error; err != nil {
		return nil, err
	}
	scored := make([]NearestPoint, 0, len(points))
	for _, p := range points {
		scored = append(scored, NearestPoint{
			Point:          p,
			DistanceMeters: geometry.HaversineMeters(q, geometry.Vertex{Lng: p.Lng, Lat: p.Lat}),
		})
	}
	sort.Slice(scored, func(i, j int) bool {
		if scored[i].DistanceMeters != scored[j].DistanceMeters {
			return scored[i].DistanceMeters < scored[j].DistanceMeters
		}
		return scored[i].ID < scored[j].ID
	})
	if len(scored) > n {
		scored = scored[:n]
	}
	return scored, nil
}

// upsertPoint inserts or updates one point while the caller owns the org
// lock and the effective set. Returns "created" | "updated" | "unchanged".
func upsertPoint(tx *gorm.DB, orgID, activeSeq int64, set *EffectiveSet, item BatchItem) (string, error) {
	var p models.Point
	err := tx.Where("org_id = ? AND external_id = ?", orgID, item.ExternalID).First(&p).Error
	if errors.Is(err, gorm.ErrRecordNotFound) {
		np := models.Point{
			OrgID:        orgID,
			ExternalID:   item.ExternalID,
			Lat:          item.Lat,
			Lng:          item.Lng,
			Version:      1,
			AssignSetSeq: activeSeq,
		}
		assign := set.Assign(geometry.Vertex{Lng: item.Lng, Lat: item.Lat})
		np.RegionID = assign.RegionID
		np.RegionVersionID = assign.RegionVersionID
		if err := tx.Create(&np).Error; err != nil {
			if isDuplicateKey(err) {
				// Concurrent insert by an interleaved transaction cannot
				// happen under the org lock, but keep the guard anyway.
				return "", &ValidationError{Status: http.StatusConflict, Message: "point created concurrently; refetch and retry"}
			}
			return "", err
		}
		return "created", nil
	}
	if err != nil {
		return "", err
	}

	if coordsEqual(p.Lat, item.Lat, p.Lng, item.Lng) {
		return "unchanged", nil
	}
	if item.ExpectedVersion == nil {
		return "", &ValidationError{Status: http.StatusConflict,
			Message: fmt.Sprintf("coordinates for existing point %q differ; supply expected_version=%d to confirm the overwrite", item.ExternalID, p.Version)}
	}
	if *item.ExpectedVersion != p.Version {
		return "", &ValidationError{Status: http.StatusConflict,
			Message: fmt.Sprintf("version conflict: expected %d but point is at version %d; refetch and retry", *item.ExpectedVersion, p.Version)}
	}

	assign := set.Assign(geometry.Vertex{Lng: item.Lng, Lat: item.Lat})
	res := tx.Model(&models.Point{}).
		Where("id = ? AND version = ?", p.ID, p.Version).
		Updates(map[string]any{
			"lat":               item.Lat,
			"lng":               item.Lng,
			"version":           p.Version + 1,
			"region_id":         assign.RegionID,
			"region_version_id": assign.RegionVersionID,
			"assign_set_seq":    activeSeq,
		})
	if res.Error != nil {
		return "", res.Error
	}
	if res.RowsAffected == 0 {
		return "", &ValidationError{Status: http.StatusConflict, Message: "version conflict: point changed concurrently; refetch and retry"}
	}
	return "updated", nil
}

func rowError(r *RowResult, code, msg string) {
	r.Status = "error"
	r.ErrorCode = code
	r.Error = msg
}

func validateCoordinate(lat, lng float64) error {
	if math.IsNaN(lat) || math.IsNaN(lng) || math.IsInf(lat, 0) || math.IsInf(lng, 0) {
		return badRequest("lat/lng must be finite numbers")
	}
	if lat < -90 || lat > 90 || lng < -180 || lng > 180 {
		return badRequest("coordinate out of range: latitude in [-90,90], longitude in [-180,180], got lat=%g lng=%g", lat, lng)
	}
	return nil
}

func validateBBox(b geometry.BBox) error {
	if b.MinLat < -90 || b.MaxLat > 90 || b.MinLng < -180 || b.MaxLng > 180 {
		return badRequest("bbox coordinates out of range")
	}
	if b.MinLat > b.MaxLat {
		return badRequest("min_lat must be <= max_lat")
	}
	if b.MinLng > b.MaxLng {
		return badRequest("bbox must not cross the antimeridian: min_lng (%g) > max_lng (%g); split the query along the 180th meridian", b.MinLng, b.MaxLng)
	}
	return nil
}

func coordsEqual(lat1, lat2, lng1, lng2 float64) bool {
	const coordEps = 1e-9
	return math.Abs(lat1-lat2) < coordEps && math.Abs(lng1-lng2) < coordEps
}
