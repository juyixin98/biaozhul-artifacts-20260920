// Package worker runs the durable reassignment background loop.
package worker

import (
	"context"
	"errors"
	"log"
	"time"

	"gorm.io/gorm"

	"geoterritory/internal/engine"
	"geoterritory/internal/models"
	"geoterritory/internal/store"
)

// Worker claims reassignment jobs, fills assignments for the new immutable
// catalog in batches, and finalizes each job under the organization lock with
// an atomic catalog flip.
type Worker struct {
	db         *gorm.DB
	tick       time.Duration
	batchSize  int
	staleAfter time.Duration
	lockWaitS  int
}

// New constructs a Worker.
func New(db *gorm.DB, tick time.Duration, batchSize int, staleAfter time.Duration, lockWaitS int) *Worker {
	if batchSize <= 0 {
		batchSize = 500
	}
	return &Worker{db: db, tick: tick, batchSize: batchSize, staleAfter: staleAfter, lockWaitS: lockWaitS}
}

// Run loops until ctx is cancelled. It is safe to run multiple replicas:
// claiming is conditional, and stale jobs are reclaimed by heartbeat age.
func (w *Worker) Run(ctx context.Context) {
	t := time.NewTicker(w.tick)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			w.tickOnce(ctx)
		}
	}
}

func (w *Worker) tickOnce(ctx context.Context) {
	job, err := store.ClaimReassignJob(w.db, w.staleAfter)
	if err != nil {
		log.Printf("worker: claim error: %v", err)
		return
	}
	if job == nil {
		return
	}
	w.runJob(ctx, job)
}

// RunOnce performs one claim/process cycle. It is exported for tests and
// administrative triggering; returns true when a job was processed.
func (w *Worker) RunOnce(ctx context.Context) bool {
	job, err := store.ClaimReassignJob(w.db, w.staleAfter)
	if err != nil {
		log.Printf("worker: claim error: %v", err)
		return false
	}
	if job == nil {
		return false
	}
	w.runJob(ctx, job)
	return true
}

func (w *Worker) runJob(ctx context.Context, job *models.ReassignJob) {
	log.Printf("worker: claimed job %d org %d %d -> %d", job.ID, job.OrgID, job.FromVersion, job.ToVersion)
	if err := w.process(ctx, job); err != nil {
		if errors.Is(err, store.ErrJobStale) {
			log.Printf("worker: job %d superseded/stale, aborting without flip", job.ID)
			return
		}
		log.Printf("worker: job %d failed: %v", job.ID, err)
		_ = store.FailJob(w.db, job.ID, err.Error())
	}
}

func (w *Worker) process(ctx context.Context, job *models.ReassignJob) error {
	newCat, err := store.LoadCatalog(w.db, job.OrgID, job.ToVersion)
	if err != nil {
		return err
	}
	var processed int64
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		default:
		}
		batch, err := store.MissingPointBatch(w.db, job.OrgID, job.ToVersion, w.batchSize)
		if err != nil {
			return err
		}
		if len(batch) == 0 {
			break
		}
		if err := store.BulkInsertAssignments(w.db, job.OrgID, newCat, batch); err != nil {
			return err
		}
		processed += int64(len(batch))
		// Heartbeat doubles as ownership fencing: after a crash + reclaim, the
		// old worker loses RUNNING and must stop so its late writes cannot land.
		if err := store.HeartbeatJob(w.db, job.ID, int64(len(batch))); err != nil {
			return err
		}
		if len(batch) < w.batchSize {
			break
		}
	}

	// Finalize under the org lock: delta sweep + atomic CAS flip. The catalog
	// is re-loaded INSIDE the lock so it can never be observed half-built.
	return store.WithOrgLock(w.db, job.OrgID, w.lockWaitS, func(tx *gorm.DB) error {
		return store.FinalizeJob(tx, func(version int) (*engine.Catalog, error) {
			return store.LoadCatalog(tx, job.OrgID, version)
		}, job, w.batchSize)
	})
}
