// Package review runs persisted, resumable integrity verification jobs.
package review

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"sync"
	"time"

	"forensiccore/internal/chain"
	"forensiccore/internal/domain"
	"forensiccore/internal/evidence"
	"forensiccore/internal/hashing"
	"forensiccore/internal/idutil"
	"forensiccore/internal/securefile"

	"gorm.io/gorm"
)

// Sentinel errors.
var (
	ErrNotFound         = errors.New("review job not found")
	ErrAlreadyRunning   = errors.New("a review for this evidence is already running or completed")
	ErrIdentityMismatch = errors.New("file identity cannot be verified for resume; refusing to continue")
	ErrPrefixMismatch   = errors.New("already-processed prefix digest does not match; file was modified since the job paused")
	ErrSizeChanged      = errors.New("current file size differs from the registered baseline")
	// ErrPause may be returned by a ChunkHook to request a clean, resumable
	// interruption at the latest checkpoint.
	ErrPause = errors.New("review paused at checkpoint")
)

// ResumeRejected marks a job that cannot be safely resumed.
type ResumeRejected struct{ Reason error }

func (r *ResumeRejected) Error() string { return r.Reason.Error() }
func (r *ResumeRejected) Unwrap() error { return r.Reason }

// ChunkHook is invoked after a chunk is processed. Cancelling the context it
// receives simulates an interruption; tests use it to pause and mutate files.
type ChunkHook func(ctx context.Context, job *domain.ReviewJob, chunkIndex int, offset int64) error

// Manager owns review job lifecycle.
type Manager struct {
	db        *gorm.DB
	evidences *evidence.Service
	resolver  *securefile.Resolver
	events    *chain.Appender
	chunkSize int

	mu     sync.Mutex
	active map[string]context.CancelFunc // job id -> cancel
	wg     sync.WaitGroup

	// Hook, if set, is invoked after every processed chunk.
	Hook ChunkHook

	// flushEvery sets how often progress is persisted (number of chunks).
	flushEvery int
}

// NewManager builds a Manager.
func NewManager(db *gorm.DB, evs *evidence.Service, resolver *securefile.Resolver, events *chain.Appender, chunkSize int) *Manager {
	fe := 32
	if chunkSize > 1024*1024 {
		fe = 4 // flush roughly every 16 MiB with a 4 MiB chunk size
	}
	return &Manager{
		db:         db,
		evidences:  evs,
		resolver:   resolver,
		events:     events,
		chunkSize:  chunkSize,
		active:     make(map[string]context.CancelFunc),
		flushEvery: fe,
	}
}

// SetFlushEvery overrides how many chunks are processed between progress
// checkpoints. Mainly useful for tests with small chunk sizes.
func (m *Manager) SetFlushEvery(n int) {
	if n > 0 {
		m.flushEvery = n
	}
}

// Start creates a review job for an evidence item and runs it asynchronously.
func (m *Manager) Start(ctx context.Context, evidenceID, actor string) (*domain.ReviewJob, error) {
	ev, err := m.evidences.Get(ctx, evidenceID)
	if err != nil {
		return nil, err
	}

	var prior int64
	if err := m.db.Model(&domain.ReviewJob{}).
		Where("evidence_id = ? AND status IN ?", evidenceID,
			[]string{domain.ReviewRunning, domain.ReviewInterrupted, domain.ReviewCompleted}).
		Count(&prior).Error; err != nil {
		return nil, err
	}
	if prior > 0 {
		return nil, ErrAlreadyRunning
	}

	job := domain.ReviewJob{
		ID:               idutil.New(),
		EvidenceID:       ev.ID,
		CaseID:           ev.CaseID,
		Status:           domain.ReviewRunning,
		BaselineSHA256:   ev.SHA256,
		ExpectedSize:     ev.Size,
		ExpectedRealPath: ev.RealPath,
		ExpectedDeviceID: ev.DeviceID,
		ExpectedInode:    ev.Inode,
		StartedBy:        actor,
	}
	if err := m.db.WithContext(ctx).Create(&job).Error; err != nil {
		return nil, err
	}
	m.spawn(&job)
	return &job, nil
}

// Resume resumes a paused/failed job. Before continuing it re-opens the file
// through the whitelist resolver and verifies BOTH the file identity
// (realpath + dev/inode + size) and the already-processed prefix digest.
// Matching only the file name or size is never accepted.
func (m *Manager) Resume(ctx context.Context, jobID, actor string) (*domain.ReviewJob, error) {
	var job domain.ReviewJob
	if err := m.db.First(&job, "id = ?", jobID).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, ErrNotFound
		}
		return nil, err
	}
	if job.Status == domain.ReviewCompleted {
		return nil, errors.New("job already completed")
	}
	if m.isActive(job.ID) {
		return nil, ErrAlreadyRunning
	}

	// Validate identity + prefix synchronously so the caller learns about a
	// modified file immediately and the job is marked failed with the reason.
	if err := m.validateForResume(ctx, &job); err != nil {
		job.Status = domain.ReviewFailed
		job.ErrorMessage = err.Error()
		_ = m.db.Save(&job).Error
		return nil, err
	}
	job.Status = domain.ReviewRunning
	job.ErrorMessage = ""
	if err := m.db.Save(&job).Error; err != nil {
		return nil, err
	}
	m.spawn(&job)
	return &job, nil
}

func (m *Manager) validateForResume(ctx context.Context, job *domain.ReviewJob) error {
	f, err := m.resolver.Resolve(job.ExpectedRealPath)
	if err != nil {
		return &ResumeRejected{Reason: fmt.Errorf("%w: cannot reopen: %v", ErrIdentityMismatch, err)}
	}
	defer f.Close()
	id := f.Identity()
	if id.RealPath != job.ExpectedRealPath {
		return &ResumeRejected{Reason: fmt.Errorf("%w: real path %q != %q", ErrIdentityMismatch, id.RealPath, job.ExpectedRealPath)}
	}
	if job.ExpectedDeviceID != 0 && (id.DeviceID != job.ExpectedDeviceID || id.Inode != job.ExpectedInode) {
		return &ResumeRejected{Reason: fmt.Errorf("%w: device/inode differ (the file was replaced)", ErrIdentityMismatch)}
	}
	if id.Size != job.ExpectedSize {
		return &ResumeRejected{Reason: fmt.Errorf("%w: size %d != baseline %d", ErrSizeChanged, id.Size, job.ExpectedSize)}
	}
	if job.Offset == 0 {
		return nil
	}
	// Re-hash the persisted prefix and compare with the stored prefix digest.
	h := sha256.New()
	if _, err := f.Seek(0, io.SeekStart); err != nil {
		return err
	}
	if _, err := io.CopyN(h, f.File, job.Offset); err != nil {
		return err
	}
	got := hex.EncodeToString(h.Sum(nil))
	if got != job.PrefixSHA256 {
		return &ResumeRejected{Reason: fmt.Errorf("%w: processed prefix changed", ErrPrefixMismatch)}
	}
	return nil
}

// Get fetches a job.
func (m *Manager) Get(ctx context.Context, id string) (*domain.ReviewJob, error) {
	var job domain.ReviewJob
	if err := m.db.WithContext(ctx).First(&job, "id = ?", id).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, ErrNotFound
		}
		return nil, err
	}
	return &job, nil
}

// ListByCase returns jobs for a case.
func (m *Manager) ListByCase(ctx context.Context, caseID string) ([]domain.ReviewJob, error) {
	var out []domain.ReviewJob
	err := m.db.WithContext(ctx).Where("case_id = ?", caseID).
		Order("created_at ASC").Find(&out).Error
	return out, err
}

// Cancel asks a running job to stop at its next checkpoint.
func (m *Manager) Cancel(jobID string) error {
	m.mu.Lock()
	cancel, ok := m.active[jobID]
	m.mu.Unlock()
	if !ok {
		return ErrNotFound
	}
	cancel()
	return nil
}

// RecoverInterrupted re-enqueues jobs left "running" by a crashed process.
func (m *Manager) RecoverInterrupted(ctx context.Context) (int, error) {
	var jobs []domain.ReviewJob
	if err := m.db.WithContext(ctx).
		Where("status IN ?", []string{domain.ReviewRunning, domain.ReviewInterrupted}).
		Find(&jobs).Error; err != nil {
		return 0, err
	}
	n := 0
	for i := range jobs {
		if m.isActive(jobs[i].ID) {
			continue
		}
		// Re-validate; a file that changed while the server was down fails the
		// job instead of silently continuing.
		if err := m.validateForResume(ctx, &jobs[i]); err != nil {
			jobs[i].Status = domain.ReviewFailed
			jobs[i].ErrorMessage = err.Error()
			_ = m.db.Save(&jobs[i]).Error
			continue
		}
		m.spawn(&jobs[i])
		n++
	}
	return n, nil
}

// Shutdown cancels active jobs and waits for them to checkpoint.
func (m *Manager) Shutdown(ctx context.Context) {
	m.mu.Lock()
	for _, cancel := range m.active {
		cancel()
	}
	m.mu.Unlock()
	done := make(chan struct{})
	go func() { m.wg.Wait(); close(done) }()
	select {
	case <-done:
	case <-ctx.Done():
	}
}

func (m *Manager) isActive(id string) bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	_, ok := m.active[id]
	return ok
}

func (m *Manager) spawn(job *domain.ReviewJob) {
	ctx, cancel := context.WithCancel(context.Background())
	m.mu.Lock()
	if _, exists := m.active[job.ID]; exists {
		m.mu.Unlock()
		cancel()
		return
	}
	m.active[job.ID] = cancel
	m.mu.Unlock()

	m.wg.Add(1)
	go func(j domain.ReviewJob) {
		defer m.wg.Done()
		defer func() {
			m.mu.Lock()
			delete(m.active, j.ID)
			m.mu.Unlock()
			cancel()
		}()
		m.runJob(ctx, &j)
	}(*job)
}

func (m *Manager) runJob(ctx context.Context, job *domain.ReviewJob) {
	f, err := m.resolver.Resolve(job.ExpectedRealPath)
	if err != nil {
		m.fail(job, fmt.Errorf("reopen failed: %w", err))
		return
	}
	defer f.Close()
	id := f.Identity()

	if id.RealPath != job.ExpectedRealPath ||
		(job.ExpectedDeviceID != 0 && (id.DeviceID != job.ExpectedDeviceID || id.Inode != job.ExpectedInode)) {
		m.fail(job, fmt.Errorf("%w: file identity changed", ErrIdentityMismatch))
		return
	}
	if id.Size != job.ExpectedSize {
		m.fail(job, fmt.Errorf("%w: size changed", ErrSizeChanged))
		return
	}

	// Rebuild the prefix hash state: either from scratch or by reading the
	// already-processed prefix when resuming.
	h := sha256.New()
	startOffset := int64(0)
	if job.Offset > 0 {
		if _, err := f.Seek(0, io.SeekStart); err != nil {
			m.fail(job, err)
			return
		}
		if _, err := io.CopyN(h, f.File, job.Offset); err != nil {
			m.fail(job, err)
			return
		}
		if got := hex.EncodeToString(h.Sum(nil)); got != job.PrefixSHA256 {
			m.fail(job, fmt.Errorf("%w: processed prefix changed", ErrPrefixMismatch))
			return
		}
		// Reset the running hasher to the prefix state: h.Sum did not mutate
		// internal state, so h still covers the prefix. Seek to Offset.
		if _, err := f.Seek(job.Offset, io.SeekStart); err != nil {
			m.fail(job, err)
			return
		}
		startOffset = job.Offset
	} else {
		if _, err := f.Seek(0, io.SeekStart); err != nil {
			m.fail(job, err)
			return
		}
	}

	buf := make([]byte, m.chunkSize)
	offset := startOffset
	chunks := 0
	for offset < job.ExpectedSize {
		want := job.ExpectedSize - offset
		if want > int64(len(buf)) {
			want = int64(len(buf))
		}
		n, readErr := io.ReadFull(f.File, buf[:want])
		if n > 0 {
			h.Write(buf[:n])
			offset += int64(n)
			chunks++
		}
		if readErr != nil && readErr != io.EOF {
			if readErr == io.ErrUnexpectedEOF {
				m.fail(job, fmt.Errorf("%w: file shrank during review", ErrSizeChanged))
				return
			}
			m.fail(job, readErr)
			return
		}

		if chunks%m.flushEvery == 0 || offset >= job.ExpectedSize {
			prefix := hex.EncodeToString(h.Sum(nil))
			if err := m.flushProgress(job.ID, offset, prefix); err != nil {
				m.fail(job, err)
				return
			}
			job.Offset = offset
			job.PrefixSHA256 = prefix
		}
		if m.Hook != nil {
			if hookErr := m.Hook(ctx, job, chunks, offset); hookErr != nil {
				if errors.Is(hookErr, ErrPause) || errors.Is(ctx.Err(), context.Canceled) {
					m.persistInterruption(job)
					return
				}
				m.fail(job, hookErr)
				return
			}
		}
		if ctx.Err() != nil {
			m.persistInterruption(job)
			return
		}
	}

	// Final identity re-check, then compare against baseline.
	cur, err := f.CurrentIdentity()
	if err != nil {
		m.fail(job, err)
		return
	}
	if !cur.Equal(id) {
		m.fail(job, fmt.Errorf("%w: file changed during review", securefile.ErrFileChanged))
		return
	}
	finalDigest := hex.EncodeToString(h.Sum(nil))
	match := finalDigest == job.BaselineSHA256
	result := "match"
	if !match {
		result = "mismatch"
	}

	now := time.Now().UTC()
	var chainSeq int64
	cerr := m.db.Transaction(func(tx *gorm.DB) error {
		updates := map[string]any{
			"status":          domain.ReviewCompleted,
			"offset":          job.ExpectedSize,
			"final_sha256":    finalDigest,
			"prefix_sha256":   finalDigest,
			"result":          result,
			"error_message":   "",
			"last_chunk_time": now,
			"completed_at":    &now,
		}
		if err := tx.Model(&domain.ReviewJob{}).Where("id = ?", job.ID).Updates(updates).Error; err != nil {
			return err
		}
		ev, err := m.events.AppendOn(ctx, tx, chain.AppendInput{
			CaseID:    job.CaseID,
			EventType: domain.EventReview,
			Actor:     job.StartedBy,
			Payload: hashing.ReviewPayload{
				JobID:          job.ID,
				EvidenceID:     job.EvidenceID,
				Status:         domain.ReviewCompleted,
				FinalSHA256:    finalDigest,
				BaselineSHA256: job.BaselineSHA256,
				Match:          match,
				Result:         result,
			},
			Occurred: now,
		})
		if err != nil {
			return err
		}
		chainSeq = ev.Seq
		return tx.Model(&domain.ReviewJob{}).Where("id = ?", job.ID).Update("chain_seq", chainSeq).Error
	})
	if cerr != nil {
		m.fail(job, cerr)
		return
	}
	job.Status = domain.ReviewCompleted
	job.Offset = job.ExpectedSize
	job.FinalSHA256 = finalDigest
	job.Result = result
	job.ChainSeq = chainSeq
}

func (m *Manager) flushProgress(jobID string, offset int64, prefixSHA string) error {
	return m.db.Model(&domain.ReviewJob{}).Where("id = ?", jobID).
		Updates(map[string]any{
			"offset":          offset,
			"prefix_sha256":   prefixSHA,
			"last_chunk_time": time.Now().UTC(),
		}).Error
}

func (m *Manager) persistInterruption(job *domain.ReviewJob) {
	// Progress was flushed at the last checkpoint; mark the job as
	// interrupted so it is clearly resumable (and distinct from read errors).
	reason := "interrupted: paused; resume required"
	_ = m.db.Model(&domain.ReviewJob{}).Where("id = ?", job.ID).
		Updates(map[string]any{
			"status":        domain.ReviewInterrupted,
			"error_message": reason,
		}).Error
	job.Status = domain.ReviewInterrupted
	job.ErrorMessage = reason
}

func (m *Manager) fail(job *domain.ReviewJob, cause error) {
	_ = m.db.Model(&domain.ReviewJob{}).Where("id = ?", job.ID).
		Updates(map[string]any{
			"status":        domain.ReviewFailed,
			"error_message": cause.Error(),
		}).Error
	job.Status = domain.ReviewFailed
	job.ErrorMessage = cause.Error()
}
