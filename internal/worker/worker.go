// Package worker runs the local render workers. It claims frames with
// persistent leases and fencing generations, renders real PNG composites,
// publishes files atomically, and guarantees the ordering rules required by
// the queue: priority/FIFO dispatch, at most 2 workers, retries, crash
// continuation, late-result rejection and cancel-vs-complete single winner.
package worker

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"os"
	"sync"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/vfxqueue/renderq/internal/compose"
	"github.com/vfxqueue/renderq/internal/db/dbgen"
	"github.com/vfxqueue/renderq/internal/domain"
	"github.com/vfxqueue/renderq/internal/storage"
)

// FaultHook lets tests inject deterministic per-attempt frame failures.
type FaultHook func(jobID string, frameNo, attempt int) error

type Worker struct {
	pool      *pgxpool.Pool
	q         *dbgen.Queries
	store     *storage.Store
	workerID  string
	leaseSecs float64
	renewSecs time.Duration
	pollDelay time.Duration
	fault     FaultHook
	logger    *log.Logger
}

func New(pool *pgxpool.Pool, store *storage.Store, leaseSecs, renewSecs, pollMillis int, fault FaultHook, logger *log.Logger) *Worker {
	if logger == nil {
		logger = log.New(os.Stderr, "[worker] ", log.LstdFlags)
	}
	return &Worker{
		pool:      pool,
		q:         dbgen.New(pool),
		store:     store,
		workerID:  "w-" + uuid.NewString()[:8],
		leaseSecs: float64(leaseSecs),
		renewSecs: time.Duration(renewSecs) * time.Second,
		pollDelay: time.Duration(pollMillis) * time.Millisecond,
		fault:     fault,
		logger:    logger,
	}
}

// Run starts n worker loops (n must be <= 2, enforced by config) and blocks
// until ctx is canceled.
func (w *Worker) Run(ctx context.Context, n int) {
	var wg sync.WaitGroup
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(slot int) {
			defer wg.Done()
			w.loop(ctx, fmt.Sprintf("%s-%d", w.workerID, slot))
		}(i)
	}
	wg.Wait()
}

// RunOnce performs a single claim+render+submit cycle. It reports whether
// any frame was claimed; tests use it for deterministic step driving.
func (w *Worker) RunOnce(ctx context.Context) (bool, error) {
	return w.processOne(ctx, w.workerID+"-manual")
}

// ClaimRenderSubmit runs the claim+render+submit cycle as a specific worker
// identity. Tests use it to simulate an already-running worker that
// finishes (and submits) at a chosen moment. Returns (claimed, error).
func (w *Worker) ClaimRenderSubmit(ctx context.Context, wid string) (bool, error) {
	return w.processOne(ctx, wid)
}

func (w *Worker) loop(ctx context.Context, wid string) {
	for {
		select {
		case <-ctx.Done():
			return
		default:
		}
		worked, err := w.processOne(ctx, wid)
		if err != nil && !errors.Is(err, context.Canceled) {
			w.logger.Printf("%s: %v", wid, err)
		}
		if !worked {
			select {
			case <-ctx.Done():
				return
			case <-time.After(w.pollDelay):
			}
		}
	}
}

// processOne runs one claim+render+submit cycle. worked=false means the
// queue had no runnable frame and the loop should back off.
func (w *Worker) processOne(parent context.Context, wid string) (bool, error) {
	frame, ok, err := w.claim(parent, wid)
	if err != nil || !ok {
		return false, err
	}

	// The frame context is canceled if we lose the lease heartbeat: any
	// rendering or submission after that must stop.
	fctx, cancel := context.WithCancel(parent)
	defer cancel()
	go w.heartbeat(fctx, cancel, frame.ID, frame.Generation, wid)

	err = w.renderAndSubmit(fctx, frame, wid)
	return true, err
}

// claim atomically takes one runnable frame. The transaction deliberately
// locks ONLY the frame row; the job is flipped to running in a separate
// statement afterwards. This keeps lock ordering job-row-then-frame-row
// everywhere that locks both (submit and cancel), so claim cannot form an
// AB-BA deadlock with a concurrent cancel.
func (w *Worker) claim(ctx context.Context, wid string) (dbgen.Frame, bool, error) {
	tx, err := w.pool.Begin(ctx)
	if err != nil {
		return dbgen.Frame{}, false, err
	}
	qtx := w.q.WithTx(tx)
	frame, err := qtx.ClaimFrame(ctx, dbgen.ClaimFrameParams{
		WorkerID: wid, LeaseSeconds: w.leaseSecs,
	})
	if errors.Is(err, pgx.ErrNoRows) {
		_ = tx.Rollback(ctx)
		return dbgen.Frame{}, false, nil
	}
	if err != nil {
		_ = tx.Rollback(ctx)
		return dbgen.Frame{}, false, err
	}
	if err := tx.Commit(ctx); err != nil {
		return dbgen.Frame{}, false, err
	}

	// Best-effort promotion queued -> running. If a cancel landed in the
	// window this affects zero rows; cancel's frame requeue (or the submit
	// guards) then handles the just-claimed frame.
	if _, err := w.q.MarkJobRunning(ctx, frame.JobID); err != nil {
		return dbgen.Frame{}, false, err
	}
	return frame, true, nil
}

// heartbeat extends the lease until the frame finishes. If renewal affects
// zero rows the lease was lost (stolen after expiry or job canceled), so the
// frame context is canceled and no result may be published.
func (w *Worker) heartbeat(ctx context.Context, cancel context.CancelFunc, frameID uuid.UUID, gen int64, wid string) {
	t := time.NewTicker(w.renewSecs)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			n, err := w.q.RenewFrameLease(ctx, dbgen.RenewFrameLeaseParams{
				ID: frameID, LeaseSeconds: w.leaseSecs, Generation: gen,
			})
			if err != nil {
				if !errors.Is(err, context.Canceled) {
					w.logger.Printf("%s: renew frame %s: %v", wid, frameID, err)
				}
				continue
			}
			if n == 0 {
				w.logger.Printf("%s: lost lease on frame %s (generation %d)", wid, frameID, gen)
				cancel()
				return
			}
		}
	}
}

func (w *Worker) renderAndSubmit(ctx context.Context, frame dbgen.Frame, wid string) error {
	job, err := w.q.GetJobWithVersion(ctx, frame.JobID)
	if err != nil {
		return w.failFrame(ctx, frame, fmt.Errorf("load job: %w", err), wid)
	}
	var manifest domain.Manifest
	if err := json.Unmarshal(job.Manifest, &manifest); err != nil {
		return w.failFrame(ctx, frame, fmt.Errorf("parse frozen manifest: %w", err), wid)
	}

	// Layers come ONLY from the frozen version_resources snapshot, never
	// from the current assets table.
	resources, err := w.q.ListVersionResources(ctx, job.VersionID)
	if err != nil {
		return w.failFrame(ctx, frame, fmt.Errorf("load version resources: %w", err), wid)
	}
	blobs := make(map[string][]byte, len(resources))
	resolved := make(map[string]domain.ResolvedAsset, len(resources))
	for _, vr := range resources {
		if err := w.store.VerifyBlob(vr.Sha256); err != nil {
			return w.failFrame(ctx, frame, fmt.Errorf("layer %s: %w", vr.LayerID, err), wid)
		}
		f, err := w.store.OpenBlob(vr.Sha256)
		if err != nil {
			return w.failFrame(ctx, frame, fmt.Errorf("layer %s: %w", vr.LayerID, err), wid)
		}
		data, err := readAll(f)
		f.Close()
		if err != nil {
			return w.failFrame(ctx, frame, fmt.Errorf("layer %s: read blob: %w", vr.LayerID, err), wid)
		}
		dim, err := pngDimensions(data)
		if err != nil {
			return w.failFrame(ctx, frame, fmt.Errorf("layer %s: decode png: %w", vr.LayerID, err), wid)
		}
		blobs[vr.Sha256] = data
		resolved[vr.LayerID] = domain.ResolvedAsset{
			LayerID: vr.LayerID, AssetID: vr.AssetID.String(), SHA256: vr.Sha256,
			Width: dim.w, Height: dim.h,
		}
	}

	if w.fault != nil {
		if ferr := w.fault(frame.JobID.String(), int(frame.FrameNo), int(frame.Attempts)); ferr != nil {
			return w.failFrame(ctx, frame, ferr, wid)
		}
	}

	layers := compose.FrameLayers(&manifest, int(frame.FrameNo), blobs, resolved)
	if len(layers) != len(manifest.Layers) {
		return w.failFrame(ctx, frame, fmt.Errorf("%d of %d layers resolved for frame %d",
			len(layers), len(manifest.Layers), frame.FrameNo), wid)
	}
	pngBytes, err := compose.RenderFrame(manifest.Width, manifest.Height, layers)
	if err != nil {
		return w.failFrame(ctx, frame, err, wid)
	}

	staged, digest, size, err := w.stage(frame.JobID.String(), int(frame.FrameNo), pngBytes)
	if err != nil {
		return w.failFrame(ctx, frame, err, wid)
	}
	return w.submit(ctx, frame, job, staged, digest, size, wid)
}

// submit commits the guarded frame result, then installs the staged file.
// Zero affected rows means this worker is late (stale generation) or the job
// was canceled: the staged file is deleted and nothing is published. The job
// status flip happens in the same transaction as the frame completion, so
// cancel and complete can only produce one terminal state.
func (w *Worker) submit(ctx context.Context, frame dbgen.Frame, job dbgen.GetJobWithVersionRow, staged, digest string, size int64, wid string) error {
	tx, err := w.pool.Begin(ctx)
	if err != nil {
		_ = os.Remove(staged)
		return err
	}
	defer tx.Rollback(ctx)
	qtx := w.q.WithTx(tx)

	// Take the job row lock first: this is what makes completion and
	// cancellation mutually exclusive. A concurrent cancel that got the
	// lock first flips the job to canceled, so the CompleteFrame guard
	// below then affects zero rows and the result is discarded.
	lockedJob, err := qtx.GetJobForUpdate(ctx, frame.JobID)
	if err != nil {
		_ = os.Remove(staged)
		return err
	}

	n, err := qtx.CompleteFrame(ctx, dbgen.CompleteFrameParams{
		ID: frame.ID, Sha256: digest, SizeBytes: size, Generation: frame.Generation,
	})
	if err != nil {
		_ = os.Remove(staged)
		return err
	}
	if n == 0 {
		// Late worker (generation advanced after lease expiry) or canceled
		// job: never overwrite the newer result, never publish after
		// cancellation.
		_ = os.Remove(staged)
		w.logger.Printf("%s: submit rejected for frame %s (stale generation %d or job canceled)",
			wid, frame.ID, frame.Generation)
		return nil
	}

	counts, err := qtx.CountFrameStatuses(ctx, frame.JobID)
	if err != nil {
		return err
	}
	jobFlipped := ""
	switch {
	case counts.Succeeded == counts.Total:
		affected, err := qtx.CompleteJobIfDone(ctx, frame.JobID)
		if err != nil {
			return fmt.Errorf("flip job succeeded: %w", err)
		}
		if affected == 1 {
			jobFlipped = "succeeded"
		}
	case counts.Pending == 0 && counts.Failed > 0:
		msg := fmt.Sprintf("%d of %d frames failed after retries", counts.Failed, counts.Total)
		if _, err := qtx.FailJobIfAllAttempted(ctx, dbgen.FailJobIfAllAttemptedParams{
			ID: frame.JobID, Error: msg,
		}); err != nil {
			return fmt.Errorf("flip job failed: %w", err)
		}
		jobFlipped = "failed"
	}
	_ = lockedJob
	if err := tx.Commit(ctx); err != nil {
		_ = os.Remove(staged)
		return err
	}

	// The DB commit won against cancel, so the job cannot have been
	// canceled for this frame (cancel waits on the same row lock and now
	// sees the terminal status and affects zero rows). Install the file
	// atomically; a crash here is repaired by Recover.
	if err := w.installStaged(staged, frame.JobID.String(), int(frame.FrameNo)); err != nil {
		return fmt.Errorf("install frame file: %w", err)
	}

	if jobFlipped == "succeeded" {
		if err := w.writeSummary(ctx, job, counts); err != nil {
			w.logger.Printf("%s: summary for job %s: %v", wid, job.ID, err)
		}
	}
	return nil
}

// failFrame records a failed attempt with its locatable error. With attempts
// remaining the frame returns to 'pending' for another worker; on the final
// attempt it becomes 'failed' and the job may flip to failed.
func (w *Worker) failFrame(ctx context.Context, frame dbgen.Frame, cause error, wid string) error {
	msg := cause.Error()
	if len(msg) > 2000 {
		msg = msg[:2000]
	}
	n, err := w.q.FailFrame(ctx, dbgen.FailFrameParams{
		ID: frame.ID, LastError: msg, Generation: frame.Generation,
	})
	if err != nil {
		return err
	}
	if n == 0 {
		// Job canceled or lease lost while rendering: discard the failure.
		w.logger.Printf("%s: failure of frame %s discarded (stale/canceled)", wid, frame.ID)
		return nil
	}
	w.logger.Printf("%s: frame job=%s frame=%d attempt=%d failed: %s",
		wid, frame.JobID, frame.FrameNo, frame.Attempts, msg)

	if frame.Attempts >= frame.MaxAttempts {
		counts, cerr := w.q.CountFrameStatuses(ctx, frame.JobID)
		if cerr == nil && counts.Pending == 0 && counts.Failed > 0 {
			m := fmt.Sprintf("%d of %d frames failed after retries", counts.Failed, counts.Total)
			_, _ = w.q.FailJobIfAllAttempted(ctx, dbgen.FailJobIfAllAttemptedParams{
				ID: frame.JobID, Error: m,
			})
		}
	}
	return nil
}

// stage durably writes bytes to a temp sibling file.
func (w *Worker) stage(jobID string, frameNo int, data []byte) (path, digest string, size int64, err error) {
	dir := w.store.JobDir(jobID)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return "", "", 0, err
	}
	tmp, err := os.CreateTemp(dir, ".stage-*")
	if err != nil {
		return "", "", 0, err
	}
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		os.Remove(tmp.Name())
		return "", "", 0, err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		os.Remove(tmp.Name())
		return "", "", 0, err
	}
	if err := tmp.Close(); err != nil {
		os.Remove(tmp.Name())
		return "", "", 0, err
	}
	return tmp.Name(), sha256hex(data), int64(len(data)), nil
}

// installStaged renames the staged file to its final path and fsyncs the
// directory. Same-directory rename is atomic on POSIX.
func (w *Worker) installStaged(staged, jobID string, frameNo int) error {
	dst := w.store.FramePath(jobID, frameNo)
	if err := os.Rename(staged, dst); err != nil {
		return err
	}
	if df, err := os.Open(filepathDir(dst)); err == nil {
		_ = df.Sync()
		_ = df.Close()
	}
	return nil
}

// writeSummary emits summary.json for a succeeded job.
func (w *Worker) writeSummary(ctx context.Context, job dbgen.GetJobWithVersionRow, counts dbgen.CountFrameStatusesRow) error {
	frames, err := w.q.ListFramesOfJob(ctx, job.ID)
	if err != nil {
		return err
	}
	type frameEntry struct {
		Frame  int    `json:"frame"`
		Status string `json:"status"`
		SHA256 string `json:"sha256,omitempty"`
		Size   int64  `json:"size,omitempty"`
		File   string `json:"file,omitempty"`
		Error  string `json:"error,omitempty"`
	}
	entries := make([]frameEntry, 0, len(frames))
	for _, f := range frames {
		e := frameEntry{Frame: int(f.FrameNo), Status: f.Status, SHA256: f.OutputSha256, Size: f.OutputSize, Error: f.LastError}
		if f.Status == "succeeded" {
			e.File = fmt.Sprintf("frame_%06d.png", f.FrameNo)
		}
		entries = append(entries, e)
	}
	sum := map[string]any{
		"jobId":         job.ID.String(),
		"versionId":     job.VersionID.String(),
		"versionSha256": job.VersionSha256,
		"status":        "succeeded",
		"frameStart":    job.FrameStart,
		"frameEnd":      job.FrameEnd,
		"totalFrames":   counts.Total,
		"frames":        entries,
	}
	data, err := json.MarshalIndent(sum, "", "  ")
	if err != nil {
		return err
	}
	return w.store.PublishSummary(job.ID.String(), data)
}
