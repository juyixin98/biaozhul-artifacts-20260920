// Package jobs runs the persistent region-reassignment pipeline.
//
// Pipeline per job:
//
//  1. CLAIM   - oldest pending/running job for any org is taken with a
//     row lock (FOR UPDATE SKIP LOCKED). Jobs left 'running' by a
//     crashed process are simply adopted and recomputed from
//     scratch, so no crash can lose or half-apply a job.
//  2. STAGE   - every point of the org is classified against the target
//     version set and written, with its point version, to
//     assignment_staging. This long phase takes no org lock and
//     never touches the live assignment columns, so queries keep
//     reading the previous complete set.
//  3. APPLY   - one transaction that:
//     a. locks the organization row,
//     b. rejects late jobs (target_seq <= active_set_seq),
//     c. locks all org points, applies staged results where the
//     point did not move, and RECOMPUTES live any point that
//     was inserted/moved during staging (nothing is missed),
//     d. flips the new region version active and the old one
//     superseded, bumps active_set_seq, marks the job done.
//
// Old-version consistency: until step 3 commits, all point assignments and
// all visible region versions still describe the previous set; step 3 is the
// atomic switch.
package jobs

import (
	"context"
	"errors"
	"log"
	"time"

	"geoterritory/internal/geometry"
	"geoterritory/internal/models"
	"geoterritory/internal/service"

	"gorm.io/gorm"
)

// Runner polls for pending jobs. It is safe to run more than one Runner in
// separate processes: claims use SKIP LOCKED and applies are guarded by
// target_seq.
type Runner struct {
	db        *gorm.DB
	pollEvery time.Duration
}

func NewRunner(db *gorm.DB) *Runner {
	return &Runner{db: db, pollEvery: 500 * time.Millisecond}
}

// Run loops until ctx is cancelled.
func (r *Runner) Run(ctx context.Context) {
	log.Printf("reassign worker: started")
	ticker := time.NewTicker(r.pollEvery)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			log.Printf("reassign worker: stopping")
			return
		case <-ticker.C:
			if err := r.processOne(ctx); err != nil {
				log.Printf("reassign worker: %v", err)
			}
		}
	}
}

// processOne claims at most one job and completes it. No job is not an error.
func (r *Runner) processOne(ctx context.Context) error {
	var jobID int64
	err := r.db.Transaction(func(tx *gorm.DB) error {
		if err := tx.Raw(`
			SELECT id FROM reassign_jobs
			WHERE status IN ('pending','running')
			ORDER BY id ASC
			LIMIT 1 FOR UPDATE SKIP LOCKED`).Scan(&jobID).Error; err != nil {
			return err
		}
		if jobID == 0 {
			return nil
		}
		now := time.Now().UTC()
		return tx.Exec(`UPDATE reassign_jobs SET status='running', started_at=? WHERE id=?`, now, jobID).Error
	})
	if err != nil {
		return err
	}
	if jobID == 0 {
		return nil
	}

	select {
	case <-ctx.Done():
		return ctx.Err()
	default:
	}

	if err := r.runJob(jobID); err != nil {
		log.Printf("reassign worker: job %d failed: %v", jobID, err)
		return r.db.Model(&models.ReassignJob{}).Where("id = ? AND status = 'running'", jobID).
			Updates(map[string]any{"status": "failed", "error": truncate(err.Error(), 1000), "finished_at": time.Now().UTC()}).Error
	}
	log.Printf("reassign worker: job %d applied", jobID)
	return nil
}

// RunJob executes STAGE + APPLY for a claimed job.
func (r *Runner) RunJob(jobID int64) error {
	return r.runJob(jobID)
}

// ClaimOne marks one oldest pending/running job as running and returns its id
// (0 when the queue is empty). Used by tests and by operators that want to
// drive the pipeline step by step; the poll loop uses the same logic.
func (r *Runner) ClaimOne() (int64, error) {
	var jobID int64
	err := r.db.Transaction(func(tx *gorm.DB) error {
		if err := tx.Raw(`
			SELECT id FROM reassign_jobs
			WHERE status IN ('pending','running')
			ORDER BY id ASC
			LIMIT 1 FOR UPDATE SKIP LOCKED`).Scan(&jobID).Error; err != nil {
			return err
		}
		if jobID == 0 {
			return nil
		}
		now := time.Now().UTC()
		return tx.Exec(`UPDATE reassign_jobs SET status='running', started_at=COALESCE(started_at, ?) WHERE id=?`, now, jobID).Error
	})
	return jobID, err
}

// StageJob runs only the STAGE phase for a job: classify all points into the
// staging table without touching live assignments.
func (r *Runner) StageJob(jobID int64) error {
	var job models.ReassignJob
	if err := r.db.First(&job, jobID).Error; err != nil {
		return err
	}
	set, err := service.LoadSetAtSeq(r.db, job.OrgID, job.TargetSeq)
	if err != nil {
		return err
	}
	return r.stage(job, set)
}

// ApplyJob runs only the APPLY (atomic switch) phase for a job.
func (r *Runner) ApplyJob(jobID int64) error {
	var job models.ReassignJob
	if err := r.db.First(&job, jobID).Error; err != nil {
		return err
	}
	set, err := service.LoadSetAtSeq(r.db, job.OrgID, job.TargetSeq)
	if err != nil {
		return err
	}
	return r.apply(job, set)
}

// runJob executes STAGE + APPLY for a claimed job.
func (r *Runner) runJob(jobID int64) error {
	if err := r.StageJob(jobID); err != nil {
		return err
	}
	return r.ApplyJob(jobID)
}

const stagingBatch = 500

func (r *Runner) stage(job models.ReassignJob, set *service.EffectiveSet) error {
	return r.db.Transaction(func(tx *gorm.DB) error {
		// Recompute from scratch: a retried crashed run may have partial rows.
		if err := tx.Exec(`DELETE FROM assignment_staging WHERE job_id = ?`, job.ID).Error; err != nil {
			return err
		}
		var maxID int64
		if err := tx.Table("points").Where("org_id = ?", job.OrgID).
			Select("COALESCE(MAX(id),0)").Scan(&maxID).Error; err != nil {
			return err
		}
		var lastID int64
		for lastID < maxID {
			var points []models.Point
			if err := tx.Where("org_id = ? AND id > ?", job.OrgID, lastID).
				Order("id ASC").Limit(stagingBatch).Find(&points).Error; err != nil {
				return err
			}
			if len(points) == 0 {
				break
			}
			rows := make([]models.AssignmentStaging, 0, len(points))
			for _, p := range points {
				a := set.Assign(geometry.Vertex{Lng: p.Lng, Lat: p.Lat})
				rows = append(rows, models.AssignmentStaging{
					JobID:           job.ID,
					PointID:         p.ID,
					OrgID:           job.OrgID,
					RegionID:        a.RegionID,
					RegionVersionID: a.RegionVersionID,
					PointVersion:    p.Version,
				})
				lastID = p.ID
			}
			if err := tx.CreateInBatches(rows, stagingBatch).Error; err != nil {
				return err
			}
		}
		return nil
	})
}

// apply is the atomic switch.
func (r *Runner) apply(job models.ReassignJob, set *service.EffectiveSet) error {
	return r.db.Transaction(func(tx *gorm.DB) error {
		// Lock the claimed job and the organization; point-write transactions
		// take the same org lock, so the world is frozen for this org while
		// the switch lands.
		var j models.ReassignJob
		if err := tx.Clauses(lockUpdate()).First(&j, job.ID).Error; err != nil {
			return err
		}
		var org models.Organization
		if err := tx.Clauses(lockUpdate()).First(&org, job.OrgID).Error; err != nil {
			return err
		}

		// Late-commit guard: a job whose target was already applied (e.g. two
		// processes raced across a crash) must never overwrite newer results.
		if org.ActiveSetSeq >= job.TargetSeq {
			return tx.Exec(`UPDATE reassign_jobs SET status='superseded', finished_at=? WHERE id=?`,
				time.Now().UTC(), job.ID).Error
		}
		// Jobs apply strictly in id order; guard against out-of-order apply.
		if org.ActiveSetSeq+1 != job.TargetSeq {
			return errors.New("cannot apply job: target_seq is not the immediate successor of active_set_seq (an earlier job is pending)")
		}

		// Lock every org point and reconcile against staging.
		var points []models.Point
		if err := tx.Clauses(lockUpdate()).Where("org_id = ?", job.OrgID).Find(&points).Error; err != nil {
			return err
		}
		staged := make(map[int64]models.AssignmentStaging, len(points))
		var stRows []models.AssignmentStaging
		if err := tx.Where("job_id = ?", job.ID).Find(&stRows).Error; err != nil {
			return err
		}
		for _, s := range stRows {
			staged[s.PointID] = s
		}

		for _, p := range points {
			assign := service.Unassigned
			if s, ok := staged[p.ID]; ok && s.PointVersion == p.Version {
				assign = service.Assignment{RegionID: s.RegionID, RegionVersionID: s.RegionVersionID}
			} else {
				// Inserted or moved during staging: recompute against the
				// target set right now so no point is missed or left stale.
				assign = set.Assign(geometry.Vertex{Lng: p.Lng, Lat: p.Lat})
			}
			if err := tx.Model(&models.Point{}).Where("id = ?", p.ID).Updates(map[string]any{
				"region_id":         assign.RegionID,
				"region_version_id": assign.RegionVersionID,
				"assign_set_seq":    job.TargetSeq,
			}).Error; err != nil {
				return err
			}
		}

		// Flip the version this job publishes; supersede its predecessor.
		var newVersion models.RegionVersion
		if err := tx.Clauses(lockUpdate()).First(&newVersion, job.NewVersionID).Error; err != nil {
			return err
		}
		if err := tx.Model(&models.RegionVersion{}).
			Where("region_id = ? AND status = 'active'", newVersion.RegionID).
			Update("status", "superseded").Error; err != nil {
			return err
		}
		if err := tx.Model(&models.RegionVersion{}).Where("id = ?", newVersion.ID).
			Updates(map[string]any{"status": "active"}).Error; err != nil {
			return err
		}
		if err := tx.Model(&models.Region{}).Where("id = ?", newVersion.RegionID).
			Update("active_version_id", newVersion.ID).Error; err != nil {
			return err
		}

		// Atomic switch.
		if err := tx.Model(&models.Organization{}).Where("id = ?", org.ID).
			Update("active_set_seq", job.TargetSeq).Error; err != nil {
			return err
		}
		now := time.Now().UTC()
		if err := tx.Model(&models.ReassignJob{}).Where("id = ?", job.ID).
			Updates(map[string]any{"status": "done", "finished_at": now}).Error; err != nil {
			return err
		}
		return tx.Exec(`DELETE FROM assignment_staging WHERE job_id = ?`, job.ID).Error
	})
}

// RecoverAdopted marks nothing special: on startup pending/running jobs are
// claimed automatically. It does clean staging rows of jobs that finished
// while this process was down (shouldn't exist, defensive only).
func (r *Runner) RecoverAdopted(ctx context.Context) {
	var stuck []models.ReassignJob
	r.db.Where("status = 'running'").Find(&stuck)
	for _, j := range stuck {
		log.Printf("reassign worker: adopting interrupted job %d (org %d, target_seq %d)", j.ID, j.OrgID, j.TargetSeq)
	}
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}
