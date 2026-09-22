package worker

import (
	"context"
	"errors"
	"fmt"
	"path/filepath"

	"vfxqueue/internal/compositor"
	"vfxqueue/internal/db/gen"
	"vfxqueue/internal/storage"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

// errStale is returned when the submitter's lease/generation is no longer
// authoritative (task cancelled/failed, or the frame was re-leased).
var errStale = errors.New("lease is stale")

// renderAndSubmit is the full frame lifecycle: load frozen resources, render
// the PNG to a temp file, atomically rename, then commit inside a DB tx that
// also decides the task terminal state.
func (w *Worker) renderAndSubmit(ctx context.Context, c claim) error {
	spec, order, res, err := w.loadVersion(ctx, c.VersionID)
	if err != nil {
		// Loading the frozen version is a hard error for this attempt; if it
		// keeps failing the frame exhausts retries.
		return w.submitFailure(ctx, c, fmt.Errorf("load version: %w", err))
	}

	if err := storage.EnsureDir(c.OutputDir); err != nil {
		return w.submitFailure(ctx, c, fmt.Errorf("output dir: %w", err))
	}

	// Rendering itself (CPU) is uninterruptible, but if the lease was lost or
	// the process is shutting down, stop before writing/publishing anything.
	// The commit guard below remains the authoritative protection.
	select {
	case <-ctx.Done():
		return nil
	default:
	}

	pngBytes, sum, err := compositor.RenderFrame(spec, order, res, int(c.FrameIndex))
	if err != nil {
		return w.submitFailure(ctx, c, err)
	}

	select {
	case <-ctx.Done():
		return nil
	default:
	}

	finalPath := filepath.Join(c.OutputDir, frameFileName(c.FrameIndex))
	tmpPath := finalPath + fmt.Sprintf(".tmp-%s-%d", c.LeaseToken.String()[:8], c.Generation)
	n, err := storage.SaveReader(tmpPath, byteReader(pngBytes))
	if err != nil {
		return w.submitFailure(ctx, c, fmt.Errorf("write output: %w", err))
	}
	// Rename happens BEFORE db commit: a crash between the two leaves a file
	// the reconciler cleans up; the reverse order could expose a succeeded row
	// with no file on disk.
	if err := renameOver(tmpPath, finalPath); err != nil {
		_ = storage.RemoveIfExists(tmpPath)
		return w.submitFailure(ctx, c, fmt.Errorf("rename output: %w", err))
	}

	if w.beforeCommit != nil {
		w.beforeCommit(c.FrameID)
	}

	committed, err := w.submitSuccess(ctx, c, finalPath, sum, n)
	if err != nil {
		if errors.Is(err, errStale) {
			// Our result must not survive when another generation owns the frame
			// or the task is cancelled/failed. Remove our file (it belongs to our
			// generation; an active newer owner rewrites on its own commit).
			_ = storage.RemoveIfExists(finalPath)
			return nil
		}
		return err
	}
	if committed {
		w.logf("frame %s idx %d committed (task %s)", c.FrameID, c.FrameIndex, c.TaskID)
	}
	return nil
}

// loadVersion reconstructs the immutable render inputs from the frozen version
// rows and verifies every asset file still matches its recorded digest.
func (w *Worker) loadVersion(ctx context.Context, versionID uuid.UUID) (*compositor.Spec, []int, map[string]compositor.Resource, error) {
	ver, err := w.q.GetVersion(ctx, versionID)
	if err != nil {
		return nil, nil, nil, err
	}
	var spec compositor.Spec
	if err := jsonUnmarshal(ver.SpecJson, &spec); err != nil {
		return nil, nil, nil, fmt.Errorf("spec json: %w", err)
	}
	order, err := spec.DrawOrder()
	if err != nil {
		return nil, nil, nil, err
	}
	rows, err := w.q.ListVersionResources(ctx, versionID)
	if err != nil {
		return nil, nil, nil, err
	}
	res := make(map[string]compositor.Resource, len(rows))
	for _, r := range rows {
		asset, err := w.q.GetAssetByID(ctx, r.AssetID)
		if err != nil {
			return nil, nil, nil, fmt.Errorf("asset %s: %w", r.AssetID, err)
		}
		// Digest guard: a swapped/corrupt blob never silently renders.
		got, sz, err := storage.SHA256File(asset.StoragePath)
		if err != nil {
			return nil, nil, nil, fmt.Errorf("asset %s read: %w", asset.ID, err)
		}
		if got != r.AssetSha256 || got != asset.Sha256 {
			return nil, nil, nil, fmt.Errorf("asset %s digest mismatch: frozen=%s file=%s",
				asset.ID, r.AssetSha256, got)
		}
		if sz != asset.SizeBytes {
			return nil, nil, nil, fmt.Errorf("asset %s size mismatch", asset.ID)
		}
		res[r.LayerID] = compositor.Resource{
			LayerID: r.LayerID, AssetPath: asset.StoragePath,
			SHA256: got, Width: int(asset.Width), Height: int(asset.Height),
		}
	}
	return &spec, order, res, nil
}

// submitSuccess commits a rendered frame and, when this was the last open
// frame, marks the task succeeded — all in one transaction holding the task
// row lock, so cancel and complete can never both win.
func (w *Worker) submitSuccess(ctx context.Context, c claim, path, sum string, size int64) (bool, error) {
	tx, err := w.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return false, err
	}
	defer tx.Rollback(ctx)
	qtx := w.q.WithTx(tx)

	// Lock the task row first: concurrent cancellation serializes here.
	locked, err := qtx.LockTaskForFrame(ctx, c.FrameID)
	if err != nil {
		return false, err
	}
	if locked.Status == "cancelled" || locked.Status == "failed" {
		return false, errStale
	}

	affected, err := qtx.SubmitFrameSuccessUpdate(ctx, gen.SubmitFrameSuccessUpdateParams{
		FrameID: c.FrameID, LeaseToken: c.LeaseToken, Generation: c.Generation,
		OutputPath: path, OutputSha256: sum, OutputSizeBytes: size,
	})
	if err != nil {
		return false, err
	}
	if affected == 0 {
		// Stale lease/generation: this worker is a late submitter.
		return false, errStale
	}

	// Sequential statement (not a CTE — data-modifying CTEs share one snapshot
	// and could not see the frame update above), so this correctly observes
	// the frame we just flipped to succeeded.
	open, err := qtx.CountOpenFramesForTask(ctx, c.TaskID)
	if err != nil {
		return false, err
	}
	if open == 0 {
		if _, err := qtx.CompleteTaskIfAllDone(ctx, c.TaskID); err != nil {
			return false, err
		}
	}
	if err := tx.Commit(ctx); err != nil {
		return false, err
	}
	return true, nil
}

// submitFailure records the failed attempt. If the frame is out of retries the
// task fails and all sibling pending/leased frames are cancelled.
func (w *Worker) submitFailure(ctx context.Context, c claim, renderErr error) error {
	tx, err := w.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	qtx := w.q.WithTx(tx)

	affected, err := qtx.SubmitFrameFailure(ctx, gen.SubmitFrameFailureParams{
		FrameID: c.FrameID, LeaseToken: c.LeaseToken, Generation: c.Generation,
		MaxAttempts: MaxAttempts, Error: renderErr.Error(),
	})
	if err != nil {
		return err
	}
	if affected == 0 {
		// Late failure: someone else owns the frame now. Must not publish.
		_ = tx.Commit(ctx)
		return errStale
	}
	fr, err := qtx.GetFrame(ctx, c.FrameID)
	if err != nil {
		return err
	}
	if fr.Status == "failed" {
		errMsg := renderErr.Error()
		if _, err := qtx.FailTask(ctx, gen.FailTaskParams{
			ID: c.TaskID, Error: &errMsg,
		}); err != nil {
			return err
		}
		if _, err := qtx.CancelRemainingFrames(ctx, c.TaskID); err != nil {
			return err
		}
	}
	return tx.Commit(ctx)
}
