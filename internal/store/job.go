package store

import (
	"errors"
	"time"

	"gorm.io/gorm"

	"geoterritory/geometry"
	"geoterritory/internal/engine"
	"geoterritory/internal/models"
)

func p2c(p models.Point) geometry.LatLng {
	return geometry.LatLng{Lat: p.Lat, Lng: p.Lng}
}

// ErrJobStale means a worker heartbeat lost ownership of the job (another
// worker reclaimed it after a crash). The old worker must stop immediately so
// a late batch can never overwrite the new result.
var ErrJobStale = errors.New("reassignment job ownership lost")

// ClaimReassignJob atomically claims the next PENDING job, or reclaims a
// RUNNING job whose heartbeat is older than staleAfter (crash recovery).
// Returns nil job when there is nothing to do.
func ClaimReassignJob(gdb *gorm.DB, staleAfter time.Duration) (*models.ReassignJob, error) {
	var job models.ReassignJob
	now := time.Now().UTC()
	cutoff := now.Add(-staleAfter)
	err := gdb.Where("status = ?", models.JobPending).
		Or("status = ? AND (heartbeat_at IS NULL OR heartbeat_at < ?)", models.JobRunning, cutoff).
		Order("id ASC").First(&job).Error
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	// Conditional claim: only flip rows still in a claimable state. Multiple
	// workers racing here resolve to exactly one winner.
	res := gdb.Model(&models.ReassignJob{}).
		Where("id = ? AND (status = ? OR (status = ? AND (heartbeat_at IS NULL OR heartbeat_at < ?)))",
			job.ID, models.JobPending, models.JobRunning, cutoff).
		Updates(map[string]any{
			"status":       models.JobRunning,
			"heartbeat_at": now,
			"updated_at":   now,
		})
	if res.Error != nil {
		return nil, res.Error
	}
	if res.RowsAffected == 0 {
		return nil, nil
	}
	job.Status = models.JobRunning
	return &job, nil
}

// HeartbeatJob refreshes ownership and advances the processed counter. It
// fails with ErrJobStale if the job is no longer RUNNING under this worker
// (e.g. reclaimed then superseded after a crash).
func HeartbeatJob(gdb *gorm.DB, jobID uint64, processedDelta int64) error {
	res := gdb.Exec(`UPDATE reassignment_jobs
		SET heartbeat_at = UTC_TIMESTAMP(6),
		    processed_points = processed_points + ?,
		    updated_at = UTC_TIMESTAMP(6)
		WHERE id = ? AND status = ?`, processedDelta, jobID, models.JobRunning)
	if res.Error != nil {
		return res.Error
	}
	if res.RowsAffected == 0 {
		return ErrJobStale
	}
	return nil
}

// MissingPointBatch returns up to limit points in the org that have no
// assignment row for catalogVersion, in a stable order. The worker drains
// these in batches; the final delta sweep under the org lock guarantees no
// point that moved/arrived during the run is missed.
func MissingPointBatch(gdb *gorm.DB, orgID uint64, catalogVersion int, limit int) ([]models.Point, error) {
	var pts []models.Point
	err := gdb.Table("points AS p").
		Select("p.*").
		Joins(`LEFT JOIN point_assignments AS pa
			ON pa.point_id = p.id AND pa.org_id = p.org_id AND pa.catalog_version = ?`, catalogVersion).
		Where("p.org_id = ? AND pa.point_id IS NULL", orgID).
		Order("p.id ASC").Limit(limit).Scan(&pts).Error
	return pts, err
}

// BulkInsertAssignments writes worker-computed assignments with INSERT
// IGNORE: it never overwrites a row the point-writer path already produced
// for the newest coordinate. Idempotent across crash restarts.
func BulkInsertAssignments(gdb *gorm.DB, orgID uint64, cat *engine.Catalog, pts []models.Point) error {
	if len(pts) == 0 {
		return nil
	}
	now := time.Now().UTC()
	sqlStr := `INSERT IGNORE INTO point_assignments
		(point_id, org_id, catalog_version, region_id, region_version_id, region_version, on_boundary, updated_at)
		VALUES `
	args := make([]any, 0, len(pts)*8)
	for i := range pts {
		if i > 0 {
			sqlStr += ","
		}
		sqlStr += "(?, ?, ?, ?, ?, ?, ?, ?)"
		d := cat.AssignPoint(p2c(pts[i]))
		args = append(args, pts[i].ID, orgID, cat.Version, d.RegionID, d.RegionVersionID,
			d.RegionVersion, d.OnBoundary, now)
	}
	return gdb.Exec(sqlStr, args...).Error
}

// FinalizeJob runs under the org lock:
//  1. re-checks ownership (a reclaimed-then-restarted late job must not flip);
//  2. drains any points still missing an assignment (arrived/moved during the
//     long running phase while the lock prevented writers);
//  3. atomically flips current -> toVersion via CAS, so a stale job for an
//     older target version can never overwrite a newer catalog;
//  4. marks the job DONE.
func FinalizeJob(tx *gorm.DB, gdbForCatalog func(version int) (*engine.Catalog, error),
	job *models.ReassignJob, batchSize int) error {

	// (1) ownership + target validation.
	var st models.CatalogState
	if err := tx.Where("org_id = ?", job.OrgID).First(&st).Error; err != nil {
		return err
	}
	if st.PublishedVersion != job.ToVersion {
		// A newer publish superseded this job; its result must never land.
		return ErrJobStale
	}

	// (2) delta sweep while holding the lock.
	newCat, err := gdbForCatalog(job.ToVersion)
	if err != nil {
		return err
	}
	for {
		var pts []models.Point
		if err := tx.Table("points AS p").
			Select("p.*").
			Joins(`LEFT JOIN point_assignments AS pa
				ON pa.point_id = p.id AND pa.org_id = p.org_id AND pa.catalog_version = ?`, job.ToVersion).
			Where("p.org_id = ? AND pa.point_id IS NULL", job.OrgID).
			Order("p.id ASC").Limit(batchSize).Scan(&pts).Error; err != nil {
			return err
		}
		if len(pts) == 0 {
			break
		}
		if err := bulkInsertAssignmentsTx(tx, job.OrgID, newCat, pts); err != nil {
			return err
		}
		if len(pts) < batchSize {
			break
		}
	}

	// (3) atomic flip with a CAS on current == from.
	now := time.Now().UTC()
	res := tx.Model(&models.CatalogState{}).
		Where("org_id = ? AND current_version = ?", job.OrgID, job.FromVersion).
		Updates(map[string]any{"current_version": job.ToVersion, "updated_at": now})
	if res.Error != nil {
		return res.Error
	}
	if res.RowsAffected == 0 {
		// Current pointer already moved; this job is late. Never overwrite.
		return ErrJobStale
	}

	// (4) close the job; counters reflect the point set that actually exists
	// now (points may have been imported during the run).
	var finalCount int64
	if err := tx.Model(&models.Point{}).Where("org_id = ?", job.OrgID).Count(&finalCount).Error; err != nil {
		return err
	}
	cerr := tx.Model(&models.ReassignJob{}).
		Where("id = ? AND status = ?", job.ID, models.JobRunning).
		Updates(map[string]any{
			"status":           models.JobDone,
			"completed_at":     now,
			"heartbeat_at":     now,
			"updated_at":       now,
			"total_points":     finalCount,
			"processed_points": finalCount,
		}).Error
	if cerr != nil {
		return cerr
	}
	return nil
}

func bulkInsertAssignmentsTx(tx *gorm.DB, orgID uint64, cat *engine.Catalog, pts []models.Point) error {
	if len(pts) == 0 {
		return nil
	}
	now := time.Now().UTC()
	sqlStr := `INSERT IGNORE INTO point_assignments
		(point_id, org_id, catalog_version, region_id, region_version_id, region_version, on_boundary, updated_at)
		VALUES `
	args := make([]any, 0, len(pts)*8)
	for i := range pts {
		if i > 0 {
			sqlStr += ","
		}
		sqlStr += "(?, ?, ?, ?, ?, ?, ?, ?)"
		d := cat.AssignPoint(p2c(pts[i]))
		args = append(args, pts[i].ID, orgID, cat.Version, d.RegionID, d.RegionVersionID,
			d.RegionVersion, d.OnBoundary, now)
	}
	return tx.Exec(sqlStr, args...).Error
}

// FailJob marks a claimed job FAILED, releasing the org for new publishes.
func FailJob(gdb *gorm.DB, jobID uint64, cause string) error {
	now := time.Now().UTC()
	return gdb.Model(&models.ReassignJob{}).
		Where("id = ? AND status = ?", jobID, models.JobRunning).
		Updates(map[string]any{
			"status":       models.JobFailed,
			"error":        cause,
			"completed_at": now,
			"updated_at":   now,
		}).Error
}

// GetJob reads one job scoped to an org.
func GetJob(gdb *gorm.DB, orgID, jobID uint64) (*models.ReassignJob, error) {
	var job models.ReassignJob
	err := gdb.Where("id = ? AND org_id = ?", jobID, orgID).First(&job).Error
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return nil, ErrNotFound
	}
	return &job, err
}
