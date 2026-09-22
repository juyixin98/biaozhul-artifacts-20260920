package api

import (
	"errors"
	"net/http"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"

	"geoterritory/geometry"
	"geoterritory/internal/store"
)

const maxBatchRows = 1000

type pointInputDTO struct {
	ExternalID      string  `json:"external_id"`
	Lat             float64 `json:"lat"`
	Lng             float64 `json:"lng"`
	ExpectedVersion *int    `json:"expected_version"`
}

type batchPointsRequest struct {
	Points []pointInputDTO `json:"points"`
}

// batchPoints imports up to 1000 points idempotently by external id.
//
// Semantics (see API.md):
//   - duplicate external id with the SAME coordinates is idempotent (no error,
//     no version bump);
//   - coordinates changed without the expected version -> VERSION_CONFLICT;
//   - expected_version 0 means insert-only, N means the row must be at seq N;
//   - valid rows are committed even when other rows fail (per-row results).
func (s *Server) batchPoints(c *gin.Context) {
	org := orgFrom(c)
	var req batchPointsRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "invalid JSON body: " + err.Error()})
		return
	}
	if len(req.Points) == 0 {
		c.JSON(http.StatusBadRequest, gin.H{"error": "points must contain at least one row"})
		return
	}
	if len(req.Points) > maxBatchRows {
		c.JSON(http.StatusBadRequest, gin.H{"error": "batch exceeds limit of 1000 rows", "max_rows": maxBatchRows})
		return
	}

	// Validate every row up front; invalid rows are reported and skipped, the
	// rest are written.
	rows := make([]store.PointRow, 0, len(req.Points))
	preResults := make(map[int]store.PointResult)
	for i, p := range req.Points {
		res := store.PointResult{Index: i, ExternalID: p.ExternalID}
		if len(p.ExternalID) == 0 || len(p.ExternalID) > 128 {
			res.Status = store.RowErrValidation
			res.Error = "external_id required (max 128 chars)"
			preResults[i] = res
			continue
		}
		if err := geometry.ValidateCoordinate(geometry.LatLng{Lat: p.Lat, Lng: p.Lng}); err != nil {
			res.Status = store.RowErrValidation
			res.Error = err.Error()
			preResults[i] = res
			continue
		}
		if p.ExpectedVersion != nil && *p.ExpectedVersion < 0 {
			res.Status = store.RowErrValidation
			res.Error = "expected_version must be >= 0"
			preResults[i] = res
			continue
		}
		rows = append(rows, store.PointRow{
			ExternalID:      p.ExternalID,
			Lat:             p.Lat,
			Lng:             p.Lng,
			ExpectedVersion: p.ExpectedVersion,
		})
	}

	var dbResults []store.PointResult
	if len(rows) > 0 {
		err := store.WithOrgLock(s.db, org.ID, s.cfg.LockTimeoutS, func(tx *gorm.DB) error {
			st, err := store.CatalogStateRow(tx, org.ID)
			if err != nil {
				return err
			}
			currentCat, err := store.LoadCatalog(tx, org.ID, st.CurrentVersion)
			if err != nil {
				return err
			}
			publishedCat := currentCat
			if st.PublishedVersion != st.CurrentVersion {
				publishedCat, err = store.LoadCatalog(tx, org.ID, st.PublishedVersion)
				if err != nil {
					return err
				}
			}
			dbResults, err = store.BatchUpsertPoints(tx, org.ID, rows, currentCat, publishedCat)
			return err
		})
		if errors.Is(err, store.ErrLockBusy) {
			c.JSON(http.StatusConflict, gin.H{"error": "organization is busy with another mutating operation, retry shortly"})
			return
		}
		if err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
			return
		}
	}

	// Merge pre-validation failures (keyed by original index) with DB results
	// (contiguous, for valid rows).
	merged := make([]store.PointResult, len(req.Points))
	dbIdx := 0
	for i := range req.Points {
		if r, ok := preResults[i]; ok {
			merged[i] = r
		} else {
			merged[i] = dbResults[dbIdx]
			merged[i].Index = i
			dbIdx++
		}
	}
	success, failed := 0, 0
	for _, r := range merged {
		if r.Status == store.RowOK {
			success++
		} else {
			failed++
		}
	}
	// 207-ish semantics expressed as 200 with explicit per-row statuses: the
	// batch as a whole was processed; the caller inspects results[].
	c.JSON(http.StatusOK, gin.H{
		"total":   len(merged),
		"success": success,
		"failed":  failed,
		"results": merged,
	})
}
