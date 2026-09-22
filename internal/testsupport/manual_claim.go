package testsupport

import (
	"encoding/json"
	"fmt"
	"path/filepath"

	"vfxqueue/internal/compositor"
	"vfxqueue/internal/db/gen"
	"vfxqueue/internal/storage"

	"github.com/google/uuid"
)

// ClaimOnce executes a single ClaimNextFrame in its own transaction, the same
// way a worker loop would.
func (h *Harness) ClaimOnce(token uuid.UUID, workerID string) (gen.ClaimNextFrameRow, error) {
	tx, err := h.Pool.Begin(h.Ctx)
	if err != nil {
		return gen.ClaimNextFrameRow{}, err
	}
	defer tx.Rollback(h.Ctx)
	row, err := h.Q.WithTx(tx).ClaimNextFrame(h.Ctx, gen.ClaimNextFrameParams{
		LeaseToken: &token,
		LeasedBy:   &workerID,
		Attempts:   3,
		Secs:       h.Config().LeaseTTL.Seconds(),
	})
	if err != nil {
		return gen.ClaimNextFrameRow{}, err
	}
	if err := tx.Commit(h.Ctx); err != nil {
		return gen.ClaimNextFrameRow{}, err
	}
	return row, nil
}

// SubmitSuccess runs the worker's success-commit logic against one frame and
// reports whether the frame row actually accepted this submitter (false means
// the lease/generation was stale).
func (h *Harness) SubmitSuccess(frameID, token uuid.UUID, generation int64,
	path, sum string, size int64) bool {
	tx, err := h.Pool.Begin(h.Ctx)
	if err != nil {
		h.T.Fatal(err)
	}
	defer tx.Rollback(h.Ctx)
	qtx := h.Q.WithTx(tx)

	locked, err := qtx.LockTaskForFrame(h.Ctx, frameID)
	if err != nil {
		// Lock fails only on a genuine DB problem here.
		h.T.Fatalf("lock task: %v", err)
	}
	if locked.Status == "cancelled" || locked.Status == "failed" {
		_ = tx.Rollback(h.Ctx)
		return false
	}
	affected, err := qtx.SubmitFrameSuccessUpdate(h.Ctx, gen.SubmitFrameSuccessUpdateParams{
		FrameID: frameID, LeaseToken: token, Generation: generation,
		OutputPath: path, OutputSha256: sum, OutputSizeBytes: size,
	})
	if err != nil {
		h.T.Fatalf("submit: %v", err)
	}
	if affected == 0 {
		_ = tx.Rollback(h.Ctx)
		return false
	}
	open, err := qtx.CountOpenFramesForTask(h.Ctx, locked.ID)
	if err != nil {
		h.T.Fatal(err)
	}
	if open == 0 {
		if _, err := qtx.CompleteTaskIfAllDone(h.Ctx, locked.ID); err != nil {
			h.T.Fatal(err)
		}
	}
	if err := tx.Commit(h.Ctx); err != nil {
		h.T.Fatal(err)
	}
	return true
}

// RenderVersionFrame performs a real composite for one frame of a frozen
// version, writes the file to the task output dir, and returns its metadata.
func (h *Harness) RenderVersionFrame(ver gen.CompositionVersion, frameIndex int) (path, sum string, size int64) {
	var spec compositor.Spec
	if err := json.Unmarshal(ver.SpecJson, &spec); err != nil {
		h.T.Fatal(err)
	}
	order, err := spec.DrawOrder()
	if err != nil {
		h.T.Fatal(err)
	}
	rows, err := h.Q.ListVersionResources(h.Ctx, ver.ID)
	if err != nil {
		h.T.Fatal(err)
	}
	res := map[string]compositor.Resource{}
	for _, r := range rows {
		a, err := h.Q.GetAssetByID(h.Ctx, r.AssetID)
		if err != nil {
			h.T.Fatal(err)
		}
		res[r.LayerID] = compositor.Resource{LayerID: r.LayerID, AssetPath: a.StoragePath, SHA256: a.Sha256}
	}
	data, s, err := compositor.RenderFrame(&spec, order, res, frameIndex)
	if err != nil {
		h.T.Fatal(err)
	}
	// Place under a directory named for the version (tests don't care about
	// the exact dir, only that the file persists).
	dir := filepath.Join(h.OutputsDir(), "manual-"+ver.ID.String())
	if err := storage.EnsureDir(dir); err != nil {
		h.T.Fatal(err)
	}
	p := filepath.Join(dir, fmt.Sprintf("frame_%06d.png", frameIndex))
	if err := storage.AtomicWriteFile(p, data); err != nil {
		h.T.Fatal(err)
	}
	return p, s, int64(len(data))
}
