// Package reconcile repairs database/filesystem divergence after crashes or
// process restarts:
//
//   - open leases are stale (the owning process is dead) -> frames re-queued,
//     generation bumped;
//   - frames marked succeeded whose output file is missing/corrupt -> re-queued;
//   - leftover .tmp-* files -> deleted;
//   - output files with no succeeded frame row -> deleted (orphans);
//   - tasks wrongly left non-terminal with pending work -> re-queued;
//   - tasks with no pending/leased work but non-terminal status -> fixed to
//     succeeded or failed according to frame states.
//
// It never converts a partially complete task into a success on its own.
package reconcile

import (
	"context"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"strings"

	"vfxqueue/internal/compositor"
	"vfxqueue/internal/db/gen"
	"vfxqueue/internal/storage"

	"github.com/jackc/pgx/v5/pgxpool"
)

type Reconciler struct {
	pool       *pgxpool.Pool
	q          *gen.Queries
	outputsDir string
	logf       func(string, ...any)
}

func New(pool *pgxpool.Pool, q *gen.Queries, outputsDir string) *Reconciler {
	return &Reconciler{pool: pool, q: q, outputsDir: outputsDir, logf: log.Printf}
}

// Run performs one reconciliation pass. Must run before workers start.
func (r *Reconciler) Run(ctx context.Context) error {
	if err := storage.EnsureDir(r.outputsDir); err != nil {
		return err
	}

	// 1. Every open lease belongs to a dead process.
	leased, err := r.q.RecoverInterruptedLeases(ctx)
	if err != nil {
		return fmt.Errorf("recover leases: %w", err)
	}
	if len(leased) > 0 {
		r.logf("reconcile: reset %d stale leases", len(leased))
	}

	// 2. Verify every succeeded frame has a decodable, matching output file.
	succeeded, err := r.q.ListSucceededFrames(ctx)
	if err != nil {
		return err
	}
	var reset int
	for _, f := range succeeded {
		bad := false
		if f.OutputPath == nil || f.OutputSha256 == nil || f.OutputSizeBytes == nil {
			bad = true
		} else {
			got, sz, verr := compositor.VerifyOutput(*f.OutputPath)
			if verr != nil || got != *f.OutputSha256 || sz != *f.OutputSizeBytes {
				bad = true
			}
		}
		if bad {
			if f.OutputPath != nil {
				_ = storage.RemoveIfExists(*f.OutputPath)
			}
			if err := r.q.ResetFrameForRecovery(ctx, f.ID); err != nil {
				return err
			}
			reset++
		}
	}
	if reset > 0 {
		r.logf("reconcile: re-queued %d succeeded frames with bad output files", reset)
	}

	// 3. Filesystem sweep: remove temp files and orphaned outputs.
	taskDirs, err := os.ReadDir(r.outputsDir)
	if err != nil {
		return err
	}
	for _, td := range taskDirs {
		if !td.IsDir() {
			continue
		}
		dir := filepath.Join(r.outputsDir, td.Name())
		files, err := os.ReadDir(dir)
		if err != nil {
			return err
		}
		for _, f := range files {
			if f.IsDir() {
				continue
			}
			full := filepath.Join(dir, f.Name())
			if strings.HasPrefix(f.Name(), ".tmp-") {
				_ = os.Remove(full)
				continue
			}
			// Orphan: no succeeded frame row carries this exact path.
			pathCopy := full
			ok, err := r.q.SucceededFrameExistsAtPath(ctx, &pathCopy)
			if err != nil {
				return err
			}
			if !ok {
				_ = os.Remove(full)
			}
		}
	}

	// 4. Task states must agree with frame states.
	if _, err := r.q.UnfinishTasksFromFrames(ctx); err != nil {
		return err
	}
	if _, err := r.q.RefinalizeSucceededTasks(ctx); err != nil {
		return err
	}
	nFailed, err := r.q.RefinalizeFailedTasks(ctx)
	if err != nil {
		return err
	}
	if nFailed > 0 {
		failedIDs, err := r.q.ListFailedTaskIDs(ctx)
		if err != nil {
			return err
		}
		for _, id := range failedIDs {
			if _, err := r.q.CancelRemainingFrames(ctx, id); err != nil {
				return err
			}
		}
		r.logf("reconcile: marked %d tasks failed based on frame states", nFailed)
	}
	r.logf("reconcile complete")
	return nil
}
