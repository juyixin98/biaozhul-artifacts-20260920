// Package worker runs the local rendering workers: it claims frames with
// persistent leases, reads frozen PNG resources, performs real alpha
// compositing, and commits results under lease+generation guards.
package worker

import (
	"context"
	"errors"
	"fmt"
	"log"
	"strings"
	"sync"
	"time"

	"vfxqueue/internal/config"
	"vfxqueue/internal/db/gen"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// MaxAttempts is the per-frame retry cap (first try + 2 retries = 3).
const MaxAttempts int16 = 3

// Worker is the frame-rendering subsystem. At most cfg.WorkerConcurrency
// (clamped to 2) frames are rendered in parallel inside this process.
type Worker struct {
	pool   *pgxpool.Pool
	q      *gen.Queries
	cfg    config.Config
	now    func() time.Time
	hostID string // stable identity for leased_by
	logf   func(format string, args ...any)

	// Hooks used by tests to simulate faults.
	beforeCommit func(frameID uuid.UUID)
}

func New(pool *pgxpool.Pool, q *gen.Queries, cfg config.Config) *Worker {
	host, _ := uuid.NewRandom()
	return &Worker{
		pool:   pool,
		q:      q,
		cfg:    cfg,
		now:    time.Now,
		hostID: "w-" + host.String()[:8],
		logf:   log.Printf,
	}
}

// Run starts the worker loops until ctx is canceled.
func (w *Worker) Run(ctx context.Context) {
	n := w.cfg.WorkerConcurrency
	if n > 2 {
		n = 2
	}
	w.logf("worker starting: %d parallel renderers, lease ttl=%s", n, w.cfg.LeaseTTL)
	var wg sync.WaitGroup
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(slot int) {
			defer wg.Done()
			w.loop(ctx, slot)
		}(i)
	}
	<-ctx.Done()
	wg.Wait()
}

func (w *Worker) loop(ctx context.Context, slot int) {
	ticker := time.NewTicker(w.cfg.PollInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
		claimed, err := w.claim(ctx)
		if err != nil {
			if ctx.Err() != nil {
				return
			}
			if !errors.Is(err, pgx.ErrNoRows) && !isClosedErr(err) {
				w.logf("slot %d: claim: %v", slot, err)
			}
			continue
		}
		w.processOne(ctx, claimed)
	}
}

// isClosedErr recognizes pool/connection closed errors produced during
// shutdown without depending on an exported sentinel across pgx versions.
func isClosedErr(err error) bool {
	if err == nil {
		return false
	}
	s := err.Error()
	return strings.Contains(s, "closed pool") ||
		strings.Contains(s, "connection closed") ||
		strings.Contains(s, "conn closed")
}

type claim struct {
	FrameID    uuid.UUID
	TaskID     uuid.UUID
	VersionID  uuid.UUID
	OutputDir  string
	FrameIndex int32
	Generation int64
	LeaseToken uuid.UUID
}

func (w *Worker) claim(ctx context.Context) (claim, error) {
	token := uuid.New()
	workerID := w.hostID
	tx, err := w.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return claim{}, err
	}
	defer tx.Rollback(ctx)
	row, err := w.q.WithTx(tx).ClaimNextFrame(ctx, gen.ClaimNextFrameParams{
		LeaseToken: &token,
		LeasedBy:   &workerID,
		Attempts:   MaxAttempts,
		Secs:       w.cfg.LeaseTTL.Seconds(),
	})
	if err != nil {
		return claim{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return claim{}, err
	}
	return claim{
		FrameID: row.FrameID, TaskID: row.TaskID, VersionID: row.VersionID,
		OutputDir: row.OutputDir, FrameIndex: row.FrameIndex,
		Generation: row.Generation, LeaseToken: token,
	}, nil
}

// ProcessOnce executes one already-claimed frame; exposed for deterministic
// tests. In normal operation loop() calls it.
func (w *Worker) processOne(parent context.Context, c claim) {
	ctx, cancel := context.WithCancel(parent)
	defer cancel()

	hbDone := make(chan struct{})
	go w.heartbeat(ctx, c, cancel, hbDone)
	defer func() { <-hbDone }()

	if err := w.renderAndSubmit(ctx, c); err != nil {
		w.logf("frame %s task %s idx %d failed: %v", c.FrameID, c.TaskID, c.FrameIndex, err)
	}
}

// heartbeat renews the lease until the frame is submitted. If a renewal finds
// no row (ErrNoRows), the lease was lost (expired and reclaimed, or frame
// cancelled) and rendering is canceled; the final submit is rejected by the
// generation guard regardless.
func (w *Worker) heartbeat(ctx context.Context, c claim, lost context.CancelFunc, done chan struct{}) {
	defer close(done)
	t := time.NewTicker(w.cfg.HeartbeatInterval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
		_, err := w.q.RenewFrameLease(ctx, gen.RenewFrameLeaseParams{
			ID: c.FrameID, LeaseToken: &c.LeaseToken,
			Generation: c.Generation, Secs: w.cfg.LeaseTTL.Seconds(),
		})
		if err != nil {
			if errors.Is(err, pgx.ErrNoRows) {
				w.logf("frame %s lease lost, aborting render", c.FrameID)
				lost()
				return
			}
			// Transient DB error: keep trying; submit guard is the real safety.
			w.logf("frame %s heartbeat: %v", c.FrameID, err)
		}
	}
}

func frameFileName(idx int32) string {
	return fmt.Sprintf("frame_%06d.png", idx)
}
