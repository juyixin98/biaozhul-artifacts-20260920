// Package jobs runs integrity-verification jobs as persistent, resumable work.
//
// Progress is durable: every processed chunk's digest is committed with the
// job offset before the next chunk is read. On resume the runner must prove
// BOTH that the file is the same object (resolved path, device, inode, size,
// mtime) AND that every already-processed chunk still hashes to its stored
// digest. Filename and size alone are never trusted.
package jobs

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"hash"
	"io"
	"log"
	"os"
	"sync"
	"syscall"
	"time"

	"forensiccore/internal/chain"
	"forensiccore/internal/models"
	"forensiccore/internal/safeio"

	"gorm.io/gorm"
)

// Hook is invoked after each chunk's progress has been committed. Tests use
// it to block a job mid-stream so interruption/resume can be exercised.
type Hook func(job *models.VerificationJob, chunkIndex int) error

// Runner polls for queued verification jobs and processes them. At most one
// job runs at a time within a Runner.
type Runner struct {
	DB            *gorm.DB
	EvidenceRoot  string
	DefaultChunks int
	PollInterval  time.Duration
	Hook          Hook
	Logger        *log.Logger

	ctx    context.Context
	cancel context.CancelFunc
	wg     sync.WaitGroup
}

// NewRunner constructs a runner.
func NewRunner(db *gorm.DB, evidenceRoot string, defaultChunkSize int, poll time.Duration, hook Hook, logger *log.Logger) *Runner {
	if logger == nil {
		logger = log.New(os.Stderr, "jobs: ", log.LstdFlags)
	}
	return &Runner{
		DB:            db,
		EvidenceRoot:  evidenceRoot,
		DefaultChunks: defaultChunkSize,
		PollInterval:  poll,
		Hook:          hook,
		Logger:        logger,
	}
}

// ResetStale moves jobs left in "running" state back to the queue. Called at
// startup so a process crash never strands progress (the persisted offset and
// chunk digests remain intact for identity-checked resume).
func (r *Runner) ResetStale(ctx context.Context) error {
	return r.DB.WithContext(ctx).
		Model(&models.VerificationJob{}).
		Where("status = ?", models.JobRunning).
		Updates(map[string]any{"status": models.JobQueued}).Error
}

// Start launches the polling loop.
func (r *Runner) Start(ctx context.Context) {
	r.ctx, r.cancel = context.WithCancel(ctx)
	r.wg.Add(1)
	go r.loop()
}

// Stop signals the loop to stop and waits for it. A job blocked in Hook is
// interrupted through context cancellation; its committed progress remains.
func (r *Runner) Stop() {
	if r.cancel != nil {
		r.cancel()
	}
	r.wg.Wait()
}

func (r *Runner) loop() {
	defer r.wg.Done()
	t := time.NewTicker(r.PollInterval)
	defer t.Stop()
	for {
		if r.ctx.Err() != nil {
			return
		}
		jobID, ok := r.claim()
		if !ok {
			select {
			case <-r.ctx.Done():
				return
			case <-t.C:
				continue
			}
		}
		r.process(jobID)
	}
}

// claim atomically takes the oldest queued job. It first selects a candidate
// and then performs a conditional UPDATE ... WHERE status='queued', so even
// with multiple runner instances a job can only be claimed once.
func (r *Runner) claim() (uint, bool) {
	var candidate models.VerificationJob
	if err := r.DB.Where("status = ?", models.JobQueued).Order("id ASC").First(&candidate).Error; err != nil {
		return 0, false
	}
	now := time.Now()
	res := r.DB.Model(&models.VerificationJob{}).
		Where("id = ? AND status = ?", candidate.ID, models.JobQueued).
		Updates(map[string]any{
			"status":     models.JobRunning,
			"started_at": now,
			"attempt":    gorm.Expr("attempt + 1"),
		})
	if res.Error != nil || res.RowsAffected == 0 {
		return 0, false
	}
	return candidate.ID, true
}

// ClaimJob atomically marks one specific queued job as running. It is used by
// RunJobOnce to drive a single job in a controlled context.
func (r *Runner) ClaimJob(jobID uint) error {
	res := r.DB.Model(&models.VerificationJob{}).
		Where("id = ? AND status = ?", jobID, models.JobQueued).
		Updates(map[string]any{
			"status":     models.JobRunning,
			"started_at": time.Now(),
			"attempt":    gorm.Expr("attempt + 1"),
		})
	if res.Error != nil {
		return res.Error
	}
	if res.RowsAffected == 0 {
		return fmt.Errorf("job %d is not queued", jobID)
	}
	return nil
}

// RunJobOnce claims and processes one specific job under ctx. Cancelling ctx
// interrupts the job mid-stream; committed progress stays in the database with
// the job marked "running", ready for ResetStale + a later resume.
func (r *Runner) RunJobOnce(ctx context.Context, jobID uint) error {
	if err := r.ClaimJob(jobID); err != nil {
		return err
	}
	r.processWithCtx(ctx, jobID)
	return nil
}

// Enqueue creates a new queued verification job for a case.
func Enqueue(db *gorm.DB, caseID uint, chunkSize int, actor string) (*models.VerificationJob, error) {
	var c models.Case
	if err := db.First(&c, caseID).Error; err != nil {
		return nil, err
	}
	if chunkSize <= 0 {
		return nil, fmt.Errorf("chunk size must be positive")
	}
	job := models.VerificationJob{
		CaseID:      c.ID,
		Status:      models.JobQueued,
		Size:        c.Size,
		ChunkSize:   chunkSize,
		TriggeredBy: actor,
	}
	if err := db.Create(&job).Error; err != nil {
		return nil, err
	}
	return &job, nil
}

// Resume marks a non-terminal job (stale running, queued, or failed) eligible
// again. Progress is kept; identity + prefix are re-verified on pickup.
func Resume(db *gorm.DB, caseID, jobID uint) (*models.VerificationJob, error) {
	var job models.VerificationJob
	if err := db.Where("id = ? AND case_id = ?", jobID, caseID).First(&job).Error; err != nil {
		return nil, err
	}
	switch job.Status {
	case models.JobCompletedMatch, models.JobCompletedMismatch:
		return nil, fmt.Errorf("job %d already completed", jobID)
	case models.JobQueued:
		// already eligible
	default:
		if err := db.Model(&job).Update("status", models.JobQueued).Error; err != nil {
			return nil, err
		}
	}
	return &job, nil
}

func (r *Runner) process(jobID uint) { r.processWithCtx(r.ctx, jobID) }

func (r *Runner) processWithCtx(ctx context.Context, jobID uint) {
	err := r.runJob(ctx, jobID)
	if err != nil {
		if errors.Is(err, context.Canceled) {
			// Leave the job as "running"; ResetStale/Resume will requeue it
			// with all committed progress intact.
			r.Logger.Printf("job %d interrupted, progress retained", jobID)
			return
		}
		r.Logger.Printf("job %d failed: %v", jobID, err)
	}
}

func (r *Runner) failJob(jobID uint, code, msg string) {
	now := time.Now()
	r.DB.Model(&models.VerificationJob{}).Where("id = ?", jobID).Updates(map[string]any{
		"status":      models.JobFailed,
		"error_code":  code,
		"last_error":  truncate(msg, 1000),
		"finished_at": now,
	})
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}

// runJob executes one verification, persisting progress per chunk.
func (r *Runner) runJob(ctx context.Context, jobID uint) error {
	var job models.VerificationJob
	if err := r.DB.First(&job, jobID).Error; err != nil {
		return err
	}
	var c models.Case
	if err := r.DB.First(&c, job.CaseID).Error; err != nil {
		return err
	}

	// --- 1. Re-resolve the whitelisted path and prove file identity. ---
	f, current, err := safeio.OpenVerify(r.EvidenceRoot, c.EvidenceFile)
	if err != nil {
		r.terminateWithEvent(ctx, &job, &c, models.JobFailed, models.ErrCodeIdentityMismatch,
			fmt.Sprintf("cannot re-open verified image: %v", err), "")
		return err
	}
	defer f.Close()

	identityErr := checkIdentity(&c, current)
	if identityErr != nil {
		r.terminateWithEvent(ctx, &job, &c, models.JobFailed, models.ErrCodeIdentityMismatch,
			identityErr.Error(), "")
		return identityErr
	}

	// --- 2. Load persisted chunks and re-verify the processed prefix. ---
	var stored []models.JobChunk
	if err := r.DB.Where("job_id = ?", jobID).Order("idx ASC").Find(&stored).Error; err != nil {
		return err
	}
	running := sha256.New()
	var verifiedOffset int64
	for i, ch := range stored {
		if ch.Idx != i {
			e := fmt.Errorf("chunk table not contiguous at position %d", i)
			r.terminateWithEvent(ctx, &job, &c, models.JobFailed, models.ErrCodeContentModified, e.Error(), "")
			return e
		}
		if ch.Offset != verifiedOffset {
			e := fmt.Errorf("chunk %d offset gap: stored %d expected %d", i, ch.Offset, verifiedOffset)
			r.terminateWithEvent(ctx, &job, &c, models.JobFailed, models.ErrCodeContentModified, e.Error(), "")
			return e
		}
		if err := readAndCompareChunk(f, verifiedOffset, ch.Length, ch.ChunkSHA256, running); err != nil {
			r.terminateWithEvent(ctx, &job, &c, models.JobFailed, models.ErrCodeContentModified,
				fmt.Sprintf("processed chunk %d changed: %v", i, err), "")
			return err
		}
		verifiedOffset += ch.Length
	}
	if verifiedOffset != job.Offset {
		e := fmt.Errorf("persisted offset %d does not match chunk data %d", job.Offset, verifiedOffset)
		r.terminateWithEvent(ctx, &job, &c, models.JobFailed, models.ErrCodeContentModified, e.Error(), "")
		return e
	}
	if verifiedOffset > c.Size {
		e := fmt.Errorf("offset %d beyond file size %d", verifiedOffset, c.Size)
		r.terminateWithEvent(ctx, &job, &c, models.JobFailed, models.ErrCodeIdentityMismatch, e.Error(), "")
		return e
	}

	// --- 3. Continue from the verified offset, committing each chunk. ---
	if _, err := f.Seek(verifiedOffset, io.SeekStart); err != nil {
		return err
	}
	buf := make([]byte, job.ChunkSize)
	remaining := c.Size - verifiedOffset
	nextIdx := len(stored)
	offset := verifiedOffset

	for remaining > 0 {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		want := int64(len(buf))
		if remaining < want {
			want = remaining
		}
		n, err := io.ReadFull(f, buf[:want])
		if err != nil {
			if errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF) {
				e := fmt.Errorf("%w: file truncated during verification", safeio.ErrFileChanged)
				r.terminateWithEvent(ctx, &job, &c, models.JobFailed, models.ErrCodeIdentityMismatch, e.Error(), "")
				return e
			}
			return err
		}
		running.Write(buf[:n])
		chunkSum := sha256.Sum256(buf[:n])
		chunkHex := hex.EncodeToString(chunkSum[:])

		newOffset := offset + int64(n)
		txErr := r.DB.Transaction(func(tx *gorm.DB) error {
			if err := tx.Create(&models.JobChunk{
				JobID:       job.ID,
				Idx:         nextIdx,
				Offset:      offset,
				Length:      int64(n),
				ChunkSHA256: chunkHex,
			}).Error; err != nil {
				return err
			}
			return tx.Model(&job).Update("offset", newOffset).Error
		})
		if txErr != nil {
			return txErr
		}
		offset = newOffset
		remaining -= int64(n)
		job.Offset = offset

		if r.Hook != nil {
			hooked := &job
			if err := r.Hook(hooked, nextIdx); err != nil {
				return fmt.Errorf("job hook: %w", err)
			}
		}
		nextIdx++
	}

	// --- 4. Final identity re-check through the same descriptor. ---
	endFi, err := f.Stat()
	if err != nil {
		return err
	}
	endID := identityFromFi(c.ResolvedPath, endFi)
	if endID.Inode != current.Inode || endID.Device != current.Device || endID.Size != current.Size ||
		endID.MtimeNanos != current.MtimeNanos {
		e := fmt.Errorf("%w: file changed during verification", safeio.ErrFileChanged)
		r.terminateWithEvent(ctx, &job, &c, models.JobFailed, models.ErrCodeIdentityMismatch, e.Error(), "")
		return e
	}

	digest := hex.EncodeToString(running.Sum(nil))
	status := models.JobCompletedMatch
	code, msg := "", ""
	if digest != c.SHA256 {
		status = models.JobCompletedMismatch
		code = models.ErrCodeBaselineMismatch
		msg = fmt.Sprintf("recomputed digest %s does not match baseline %s", digest, c.SHA256)
	}
	r.terminateWithEvent(ctx, &job, &c, status, code, msg, digest)
	return nil
}

// checkIdentity compares the re-opened file against registration records.
// Name/size are deliberately insufficient: device+inode pin the exact object
// and mtime detects in-place writes.
func checkIdentity(c *models.Case, cur safeio.Identity) error {
	switch {
	case cur.Device != c.Device || cur.Inode != c.Inode:
		return fmt.Errorf("%w: object identity differs (dev/inode)", safeio.ErrFileChanged)
	case cur.Size != c.Size:
		return fmt.Errorf("%w: size differs (registered %d, now %d)", safeio.ErrFileChanged, c.Size, cur.Size)
	case cur.MtimeNanos != c.MtimeNanos:
		return fmt.Errorf("%w: mtime differs (registered %d, now %d)", safeio.ErrFileChanged, c.MtimeNanos, cur.MtimeNanos)
	}
	return nil
}

func identityFromFi(path string, fi os.FileInfo) safeio.Identity {
	id := safeio.Identity{Path: path, Size: fi.Size(), MtimeNanos: fi.ModTime().UnixNano(), Mode: fi.Mode()}
	if st, ok := fi.Sys().(*syscall.Stat_t); ok {
		id.Device = uint64(st.Dev)
		id.Inode = st.Ino
	}
	return id
}

// readAndCompareChunk reads length bytes at offset, feeds h, and compares the
// chunk digest to the stored value.
func readAndCompareChunk(f *os.File, offset, length int64, expectedChunk string, h hash.Hash) error {
	if _, err := f.Seek(offset, io.SeekStart); err != nil {
		return err
	}
	buf := make([]byte, length)
	if _, err := io.ReadFull(f, buf); err != nil {
		return err
	}
	h.Write(buf)
	sum := sha256.Sum256(buf)
	if got := hex.EncodeToString(sum[:]); got != expectedChunk {
		return fmt.Errorf("chunk at offset %d: stored digest %s, recomputed %s", offset, expectedChunk, got)
	}
	return nil
}

// terminateWithEvent records the terminal job state and appends the
// verification event to the evidence chain. Terminal writes use a fresh
// context so a shutdown signal cannot strand a finished job in "running".
func (r *Runner) terminateWithEvent(_ context.Context, job *models.VerificationJob, c *models.Case, status, code, msg, digest string) {
	now := time.Now()
	updates := map[string]any{
		"status":      status,
		"error_code":  code,
		"last_error":  truncate(msg, 1000),
		"finished_at": now,
	}
	if err := r.DB.WithContext(context.Background()).Model(&models.VerificationJob{}).Where("id = ?", job.ID).Updates(updates).Error; err != nil {
		r.Logger.Printf("job %d: persist terminal state: %v", job.ID, err)
	}
	payload := map[string]any{
		"job_id":           job.ID,
		"status":           status,
		"baseline_sha256":  c.SHA256,
		"size":             c.Size,
		"chunks_processed": job.Offset,
	}
	if digest != "" {
		payload["sha256"] = digest
	}
	if code != "" {
		payload["error_code"] = code
		payload["error"] = msg
	}
	if _, err := chain.Append(r.DB.WithContext(context.Background()), chain.AppendOptions{
		CaseID:  c.ID,
		Type:    models.EventVerification,
		Actor:   job.TriggeredBy,
		Payload: payload,
	}); err != nil {
		r.Logger.Printf("job %d: append chain event: %v", job.ID, err)
	}
}
