package store

import (
	"context"
	"errors"
	"time"

	"gorm.io/gorm"

	"sitevitals/internal/models"
)

// ErrLeaseLost means the worker's fencing token no longer owns the job
// (its lease expired and was given to another worker, or the job finished).
var ErrLeaseLost = errors.New("lease lost: fencing token rejected")

// ErrNoJob means the queue is currently empty.
var ErrNoJob = errors.New("no claimable job")

// Claim is the result of atomically claiming a queued job.
type Claim struct {
	Job *models.Job
	Run *models.Run
}

// Queue implements the leasing, fenced job queue.
type Queue struct{ DB *gorm.DB }

// NewQueue constructs a Queue.
func NewQueue(db *gorm.DB) *Queue { return &Queue{DB: db} }

// Enqueue creates a job for targetURL/viewport.
func (q *Queue) Enqueue(ctx context.Context, targetURL, viewport string, maxAttempts int) (*models.Job, error) {
	if maxAttempts <= 0 {
		maxAttempts = 3
	}
	job := &models.Job{
		TargetURL:   targetURL,
		Viewport:    viewport,
		Status:      models.JobQueued,
		MaxAttempts: maxAttempts,
	}
	if err := q.DB.WithContext(ctx).Create(job).Error; err != nil {
		return nil, err
	}
	return job, nil
}

// ClaimJob atomically takes the oldest queued job for holder, assigning a new
// strictly-increasing fencing token and creating the corresponding Run row.
// Returns ErrNoJob when nothing is claimable.
//
// SKIP LOCKED lets multiple workers race the same queue without blocking.
func (q *Queue) ClaimJob(ctx context.Context, holder string, ttl time.Duration) (*Claim, error) {
	var claim *Claim
	err := q.DB.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		var job models.Job
		res := tx.Clauses(skipLocked()).
			Where("status = ?", models.JobQueued).
			Order("id ASC").
			Limit(1).
			Find(&job)
		if res.Error != nil {
			return res.Error
		}
		if res.RowsAffected == 0 || job.ID == 0 {
			return ErrNoJob
		}
		now := time.Now().UTC()
		newFence := job.FencingToken + 1
		leaseEnd := now.Add(ttl)
		newAttempt := job.Attempts + 1
		updates := map[string]any{
			"status":        models.JobRunning,
			"attempts":      gorm.Expr("attempts + ?", 1),
			"fencing_token": newFence,
			"lease_holder":  holder,
			"leased_until":  leaseEnd,
			"updated_at":    now,
		}
		if err := tx.Model(&models.Job{}).
			Where("id = ? AND status = ?", job.ID, models.JobQueued).
			UpdateColumns(updates).Error; err != nil {
			return err
		}
		run := &models.Run{
			JobID:        job.ID,
			Attempt:      newAttempt,
			Viewport:     job.Viewport,
			TargetURL:    job.TargetURL,
			Status:       models.RunRunning,
			LeaseHolder:  holder,
			FencingToken: newFence,
			StartedAt:    now,
		}
		if err := tx.Create(run).Error; err != nil {
			return err
		}
		job.Status = models.JobRunning
		job.Attempts = newAttempt
		job.FencingToken = newFence
		job.LeaseHolder = holder
		job.LeasedUntil = &leaseEnd
		claim = &Claim{Job: &job, Run: run}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return claim, nil
}

// Heartbeat extends an active lease. It returns ErrLeaseLost if the caller's
// fencing token is stale (the job was reaped and re-leased, or is terminal).
func (q *Queue) Heartbeat(ctx context.Context, jobID uint64, fence int64, holder string, ttl time.Duration) error {
	res := q.DB.WithContext(ctx).Model(&models.Job{}).
		Where("id = ? AND fencing_token = ? AND lease_holder = ? AND status = ?",
			jobID, fence, holder, models.JobRunning).
		UpdateColumn("leased_until", time.Now().UTC().Add(ttl))
	if res.Error != nil {
		return res.Error
	}
	if res.RowsAffected == 0 {
		return ErrLeaseLost
	}
	return nil
}

// ReapExpired finds running jobs whose leases expired before now and resets
// them to queued for another claim. A new claim increments the fencing token,
// so the crashed/slow worker's late writes will be rejected.
func (q *Queue) ReapExpired(ctx context.Context, now time.Time) (int64, error) {
	var n int64
	err := q.DB.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		var ids []uint64
		if err := tx.Model(&models.Job{}).
			Clauses(skipLocked()).
			Where("status = ? AND leased_until IS NOT NULL AND leased_until < ?", models.JobRunning, now).
			Pluck("id", &ids).Error; err != nil {
			return err
		}
		for _, id := range ids {
			res := tx.Model(&models.Job{}).
				Where("id = ? AND status = ? AND leased_until < ?", id, models.JobRunning, now).
				UpdateColumns(map[string]any{
					"status":       models.JobQueued,
					"lease_holder": "",
					"leased_until": nil,
					"updated_at":   now,
				})
			if res.Error != nil {
				return res.Error
			}
			n += res.RowsAffected
			if res.RowsAffected > 0 {
				// Mark every still-running attempt row as failed (crash recovery bookkeeping).
				if err := tx.Model(&models.Run{}).
					Where("job_id = ? AND status = ?", id, models.RunRunning).
					Updates(map[string]any{
						"status":       models.RunFailed,
						"fail_class":   models.FailBrowserExited,
						"fail_message": "lease expired before the worker reported; presumed crashed",
						"finished_at":  now,
					}).Error; err != nil {
					return err
				}
			}
		}
		return nil
	})
	return n, err
}

// SuccessReport is the successful collector output persisted under the run.
type SuccessReport struct {
	FinalURL         string
	RedirectHops     int
	Metrics          []*models.Metric
	Resources        []*models.ResourceEntry
	Events           []*models.RunEvent
	ResourceFailures int
	BlockedResources int
}

// ReportSuccess commits a successful run and all of its artifacts. The WHERE
// clause on fencing_token + status makes a stale (late) worker a no-op: it can
// never overwrite a newer attempt's result, and it can never create a second
// report for the same job.
//
// Returns true when the report was accepted, false when the lease was lost.
func (q *Queue) ReportSuccess(ctx context.Context, jobID uint64, runID uint64, fence int64, holder string, rep *SuccessReport) (bool, error) {
	accepted := false
	err := q.DB.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		now := time.Now().UTC()
		res := tx.Model(&models.Job{}).
			Where("id = ? AND fencing_token = ? AND lease_holder = ? AND status = ? AND succeeded_run_id IS NULL",
				jobID, fence, holder, models.JobRunning).
			UpdateColumns(map[string]any{
				"status":           models.JobSucceeded,
				"succeeded_run_id": runID,
				"leased_until":     nil,
				"last_error":       "",
				"last_fail_class":  "",
				"updated_at":       now,
			})
		if res.Error != nil {
			return res.Error
		}
		if res.RowsAffected == 0 {
			// Fence mismatch / job already terminal: the old worker must not write.
			return nil
		}
		if err := tx.Model(&models.Run{}).
			Where("id = ? AND fencing_token = ? AND status = ?", runID, fence, models.RunRunning).
			UpdateColumns(map[string]any{
				"status":            models.RunSucceeded,
				"final_url":         rep.FinalURL,
				"redirect_hops":     rep.RedirectHops,
				"resource_failures": rep.ResourceFailures,
				"blocked_resources": rep.BlockedResources,
				"finished_at":       now,
			}).Error; err != nil {
			return err
		}
		for _, m := range rep.Metrics {
			m.RunID = runID
			if err := tx.Create(m).Error; err != nil {
				return err
			}
		}
		for _, r := range rep.Resources {
			r.RunID = runID
			if err := tx.Create(r).Error; err != nil {
				return err
			}
		}
		for _, e := range rep.Events {
			e.RunID = runID
			if e.CreatedAt.IsZero() {
				e.CreatedAt = now
			}
			if err := tx.Create(e).Error; err != nil {
				return err
			}
		}
		accepted = true
		return nil
	})
	return accepted, err
}

// ReportFailure commits a failed run. With retries left the job goes back to
// queued (and the new claim bumps the fencing token); otherwise the job is
// terminal failed. Stale callers are rejected the same way as ReportSuccess.
//
// Returns true when accepted, false when the lease had been lost.
func (q *Queue) ReportFailure(ctx context.Context, jobID uint64, runID uint64, fence int64, holder, failClass, message string) (bool, error) {
	accepted := false
	err := q.DB.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		var job models.Job
		if err := tx.Where("id = ?", jobID).Take(&job).Error; err != nil {
			return err
		}
		if job.FencingToken != fence || job.LeaseHolder != holder || job.Status != models.JobRunning {
			return nil // stale/late worker: its report is discarded
		}
		now := time.Now().UTC()
		if err := tx.Model(&models.Run{}).
			Where("id = ? AND fencing_token = ? AND status = ?", runID, fence, models.RunRunning).
			UpdateColumns(map[string]any{
				"status":       models.RunFailed,
				"fail_class":   failClass,
				"fail_message": truncate(message, 500),
				"finished_at":  now,
			}).Error; err != nil {
			return err
		}
		if job.Attempts < job.MaxAttempts {
			if err := tx.Model(&models.Job{}).
				Where("id = ? AND fencing_token = ?", jobID, fence).
				UpdateColumns(map[string]any{
					"status":          models.JobQueued,
					"lease_holder":    "",
					"leased_until":    nil,
					"last_error":      truncate(message, 500),
					"last_fail_class": failClass,
					"updated_at":      now,
				}).Error; err != nil {
				return err
			}
		} else {
			if err := tx.Model(&models.Job{}).
				Where("id = ? AND fencing_token = ?", jobID, fence).
				UpdateColumns(map[string]any{
					"status":          models.JobFailed,
					"leased_until":    nil,
					"last_error":      truncate(message, 500),
					"last_fail_class": failClass,
					"updated_at":      now,
				}).Error; err != nil {
				return err
			}
		}
		accepted = true
		return nil
	})
	return accepted, err
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}
