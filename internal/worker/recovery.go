package worker

import (
	"context"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"strings"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/vfxqueue/renderq/internal/db/dbgen"
	"github.com/vfxqueue/renderq/internal/storage"
)

// Recover runs once at process startup, BEFORE any worker starts claiming.
// It reconciles the database with the filesystem so that:
//
//  1. Leases held by the dead process are invalidated (generation bump) and
//     their frames return to pending; running jobs become queued again, so
//     rendering resumes from already-completed frames.
//  2. Frames marked succeeded but whose file is missing or corrupt (crash
//     between the DB commit and the file rename) are reset to pending and
//     re-rendered; if that contradicts a 'succeeded' job, the job is
//     reopened too. Partial output is never presented as whole success.
//  3. Leftover staging files (crash mid-write) and unacknowledged frame
//     files are pruned.
func Recover(ctx context.Context, pool *pgxpool.Pool, store *storage.Store, logger *log.Logger) error {
	if logger == nil {
		logger = log.New(os.Stderr, "[recover] ", log.LstdFlags)
	}
	q := dbgen.New(pool)

	// (1) Expire every lease the dead process held.
	if err := q.RequeueAllLeasedFramesAtStartup(ctx); err != nil {
		return fmt.Errorf("expire leases: %w", err)
	}
	if err := q.RequeueRunningJobsAtStartup(ctx); err != nil {
		return fmt.Errorf("requeue running jobs: %w", err)
	}

	// (3) Sweep staging leftovers.
	if err := store.SweepTemp(); err != nil {
		logger.Printf("sweep tmp: %v", err)
	}
	if err := sweepStaged(store); err != nil {
		logger.Printf("sweep staged: %v", err)
	}

	// (2)+(4) reconcile frame files per job.
	jobIDs, err := q.ListAllJobIDs(ctx)
	if err != nil {
		return fmt.Errorf("list jobs: %w", err)
	}
	for _, jid := range jobIDs {
		if err := reconcileJob(ctx, pool, store, jid, logger); err != nil {
			logger.Printf("reconcile job %s: %v", jid, err)
		}
	}
	return nil
}

// reconcileJob restores file/DB consistency for one job.
func reconcileJob(ctx context.Context, pool *pgxpool.Pool, store *storage.Store, jobID uuid.UUID, logger *log.Logger) error {
	q := dbgen.New(pool)
	rows, err := q.ListFramesOfJobForRecovery(ctx, jobID)
	if err != nil {
		return err
	}
	acknowledged := make(map[int]bool, len(rows))
	var missing []string // frame ids the DB calls succeeded but disk lacks
	for _, fr := range rows {
		acknowledged[int(fr.FrameNo)] = true
		if fr.Status != "succeeded" {
			continue
		}
		if !store.FrameExists(jobID.String(), int(fr.FrameNo), fr.OutputSha256, fr.OutputSize) {
			missing = append(missing, fr.ID.String())
			logger.Printf("job %s frame %d: output missing/corrupt, will re-render",
				jobID, fr.FrameNo)
		}
	}

	// Prune frame files the DB does not acknowledge (orphans from an old
	// incarnation or from a frame that was reset).
	pruneOrphans(store, jobID.String(), acknowledged, logger)

	if len(missing) == 0 {
		return nil
	}

	// Reset each missing frame and, if the job had already been marked
	// succeeded, reopen it. Done in a transaction per job.
	tx, err := pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	qtx := q.WithTx(tx)
	for _, fid := range missing {
		if err := qtx.ResetSucceededFrameForRecovery(ctx, uuid.MustParse(fid)); err != nil {
			return err
		}
	}
	reopened, err := qtx.ReopenSucceededJobForRecovery(ctx, jobID)
	if err != nil {
		return err
	}
	if err := tx.Commit(ctx); err != nil {
		return err
	}
	if reopened > 0 {
		logger.Printf("job %s reopened after recovering %d missing frame(s)", jobID, len(missing))
	}
	return nil
}

// sweepStaged removes ".stage-*" files left in output dirs by a crash
// between staging and the atomic rename.
func sweepStaged(store *storage.Store) error {
	root := filepath.Join(store.Root, "outputs")
	return filepath.WalkDir(root, func(p string, d os.DirEntry, err error) error {
		if err != nil {
			if os.IsNotExist(err) {
				return nil
			}
			return err
		}
		if !d.IsDir() && strings.HasPrefix(d.Name(), ".stage-") {
			return os.Remove(p)
		}
		return nil
	})
}

// pruneOrphans deletes frame_*.png files whose frame the DB does not know as
// a current row. summary.json is left alone (it is rewritten on success and
// is harmless for in-flight jobs).
func pruneOrphans(store *storage.Store, jobID string, acknowledged map[int]bool, logger *log.Logger) {
	entries, err := os.ReadDir(store.JobDir(jobID))
	if err != nil {
		return
	}
	for _, e := range entries {
		name := e.Name()
		if e.IsDir() || !strings.HasPrefix(name, "frame_") || !strings.HasSuffix(name, ".png") {
			continue
		}
		var n int
		if _, err := fmt.Sscanf(name, "frame_%06d.png", &n); err != nil {
			continue
		}
		if !acknowledged[n] {
			if err := os.Remove(filepath.Join(store.JobDir(jobID), name)); err != nil {
				logger.Printf("prune orphan %s: %v", name, err)
			}
		}
	}
}
