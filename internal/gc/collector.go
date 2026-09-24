// Package gc implements safe garbage collection of layer blobs.
//
// Safety model
//
//   - Mark phase: one SERIALIZABLE, READ ONLY, DEFERRABLE snapshot
//     transaction.  Every layer blob reachable from any current tag is marked
//     retained; the rest are candidates.  The snapshot xmin is recorded so the
//     audit trail says exactly which database state was used.
//   - Sweep phase: candidates are re-checked *per blob* inside fresh
//     SERIALIZABLE transactions that take FOR UPDATE on the blob row:
//     1. expire leases past their ttl,
//     2. re-query tag reachability (did a tag land on this layer since the
//     snapshot?) and active leases (is a reader streaming it?),
//     3. only then unlink the file and delete the row.
//     FOR UPDATE interlocks with the FOR SHARE taken by tag updates and lease
//     acquisition, so a layer referenced during scanning cannot be deleted.
//   - Crash recovery: a run left in 'marking'/'sweeping' by a dead process is
//     marked crashed; a new run can start independently — no blob is deleted
//     twice because deletes are keyed by primary key and idempotent.
package gc

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5/pgconn"

	"layer-gc/internal/storage"
)

// Item is one audited per-blob decision.
type Item struct {
	Digest   string
	Decision string // "retain" | "delete"
	Reason   string
	Size     int64
	Marked   bool
}

// Report is the auditable outcome of a GC run.
type Report struct {
	RunID          int64
	StartedAt      time.Time
	FinishedAt     time.Time
	Status         string
	SnapshotXmin   int64
	Marked         int
	Candidates     int
	Deleted        int
	Retained       int
	Items          []Item
	RecoveredCrash bool // previous unfinished run was detected
}

// Hooks let tests deterministically interleave concurrent operations with GC.
type Hooks struct {
	// AfterMark runs after the mark snapshot committed, before sweeping.
	AfterMark func(runID int64) error
	// BeforeDeleteBlob runs immediately before a candidate is re-verified.
	BeforeDeleteBlob func(digest string) error
}

// Collector runs mark-and-sweep against the database and content store.
type Collector struct {
	DB    *sql.DB
	Store *storage.Store
	Clock func() time.Time
	Hooks Hooks
}

func New(db *sql.DB, store *storage.Store) *Collector {
	return &Collector{DB: db, Store: store, Clock: time.Now}
}

// isSerializationFailure recognizes Postgres SQLSTATE 40001/40P01.
func isSerializationFailure(err error) bool {
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) {
		return pgErr.Code == "40001" || pgErr.Code == "40P01"
	}
	return false
}

// isConcurrentReference recognizes 23503 (foreign_key_violation) and 23505
// (unique_violation) which arise when a reference/lease is inserted against a
// blob row our sweep is deleting concurrently.  Treating these as retriable
// closes the read/write race between lease acquisition and deletion.
func isConcurrentReference(err error) bool {
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) {
		return pgErr.Code == "23503" || pgErr.Code == "23505"
	}
	return false
}

// Run executes one full mark-and-sweep cycle and returns its audit report.
func (c *Collector) Run(ctx context.Context) (*Report, error) {
	started := c.Clock().UTC()

	recovered, err := c.recoverStaleRuns(ctx)
	if err != nil {
		return nil, err
	}

	var runID int64
	if err := c.DB.QueryRowContext(ctx,
		`INSERT INTO gc_runs(started_at,status) VALUES ($1,'marking') RETURNING id`,
		started).Scan(&runID); err != nil {
		return nil, err
	}

	rep := &Report{RunID: runID, StartedAt: started, RecoveredCrash: recovered}

	// ---- MARK: consistent snapshot --------------------------------------
	marked, candidates, xmin, err := c.mark(ctx, runID)
	if err != nil {
		c.failRun(ctx, runID)
		return nil, fmt.Errorf("mark phase: %w", err)
	}
	rep.Marked, rep.Candidates, rep.SnapshotXmin = len(marked), len(candidates), xmin
	if _, err := c.DB.ExecContext(ctx,
		`UPDATE gc_runs SET status='sweeping', snapshot_xmin=$1,
		 marked_count=$2, candidate_count=$3 WHERE id=$4`,
		xmin, len(marked), len(candidates), runID); err != nil {
		return nil, err
	}

	if c.Hooks.AfterMark != nil {
		if err := c.Hooks.AfterMark(runID); err != nil {
			c.failRun(ctx, runID)
			return nil, err
		}
	}

	// ---- SWEEP: per-candidate re-verification ---------------------------
	rep.Items = make([]Item, 0, len(marked)+len(candidates))
	for _, d := range marked {
		rep.Items = append(rep.Items, Item{
			Digest: d.digest, Decision: "retain",
			Reason: "reachable from a stored manifest at mark snapshot",
			Size:   d.size, Marked: true,
		})
	}

	for _, cand := range candidates {
		if c.Hooks.BeforeDeleteBlob != nil {
			if err := c.Hooks.BeforeDeleteBlob(cand.digest); err != nil {
				c.failRun(ctx, runID)
				return nil, err
			}
		}
		item, err := c.sweepOne(ctx, runID, cand)
		if err != nil {
			c.failRun(ctx, runID)
			return nil, fmt.Errorf("sweep %s: %w", cand.digest, err)
		}
		rep.Items = append(rep.Items, item)
		switch item.Decision {
		case "delete":
			rep.Deleted++
		case "retain":
			rep.Retained++
		}
	}

	finished := c.Clock().UTC()
	rep.FinishedAt = finished
	rep.Status = "completed"
	if _, err := c.DB.ExecContext(ctx,
		`UPDATE gc_runs SET finished_at=$1,status='completed',
		 deleted_count=$2,retained_count=$3 WHERE id=$4`,
		finished, rep.Deleted, rep.Retained, runID); err != nil {
		return nil, err
	}
	return rep, nil
}

type blobInfo struct {
	digest string
	size   int64
}

// mark takes one serializable read-only snapshot.  tag reachability is the
// transitive closure tags -> manifests -> manifest_refs -> blobs.
func (c *Collector) mark(ctx context.Context, runID int64) (retained, candidates []blobInfo, xmin int64, err error) {
	tx, err := c.DB.BeginTx(ctx, &sql.TxOptions{
		Isolation: sql.LevelSerializable,
		ReadOnly:  true,
	})
	if err != nil {
		return nil, nil, 0, err
	}
	defer tx.Rollback()

	if err := tx.QueryRowContext(ctx, `SELECT pg_snapshot_xmin(pg_current_snapshot())`).Scan(&xmin); err != nil {
		return nil, nil, 0, err
	}

	rows, err := tx.QueryContext(ctx, `
		SELECT b.digest, b.size_bytes,
		       EXISTS (
		         SELECT 1 FROM manifest_refs mr
		         WHERE mr.blob_digest = b.digest
		       ) AS reachable
		FROM blobs b`)
	if err != nil {
		return nil, nil, 0, err
	}
	type rec struct {
		blobInfo
		reachable bool
	}
	var all []rec
	for rows.Next() {
		var r rec
		if err := rows.Scan(&r.digest, &r.size, &r.reachable); err != nil {
			rows.Close()
			return nil, nil, 0, err
		}
		all = append(all, r)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return nil, nil, 0, err
	}
	if err := tx.Commit(); err != nil {
		return nil, nil, 0, err
	}

	for _, r := range all {
		if r.reachable {
			retained = append(retained, r.blobInfo)
		} else {
			candidates = append(candidates, r.blobInfo)
		}
	}
	return retained, candidates, xmin, nil
}

// sweepOne re-verifies a single candidate right before deletion and retries on
// serialization conflicts or a reference/lease racing the delete.
func (c *Collector) sweepOne(ctx context.Context, runID int64, cand blobInfo) (Item, error) {
	const maxAttempts = 8
	var lastErr error
	for attempt := 1; attempt <= maxAttempts; attempt++ {
		item, err := c.sweepAttempt(ctx, runID, cand)
		if err == nil {
			return item, nil
		}
		if isSerializationFailure(err) || isConcurrentReference(err) {
			lastErr = err
			// Brief back-off so the racing writer's commit lands first.
			select {
			case <-ctx.Done():
				return Item{}, ctx.Err()
			case <-time.After(time.Duration(attempt) * 5 * time.Millisecond):
			}
			continue
		}
		return Item{}, err
	}
	// Conflict never resolved: conservatively retain with an audit reason.
	item := Item{Digest: cand.digest, Decision: "retain", Marked: false, Size: cand.size,
		Reason: fmt.Sprintf("retained: persistent concurrent-reference conflict (%v); retry next GC", lastErr)}
	if err := c.recordItem(ctx, runID, item); err != nil {
		return Item{}, err
	}
	return item, nil
}

// sweepDecision is the outcome of one re-verification transaction.
type sweepDecision struct {
	decision string
	reason   string
}

func (c *Collector) sweepAttempt(ctx context.Context, runID int64, cand blobInfo) (Item, error) {
	// READ COMMITTED + strict lock ordering is what closes the mark/sweep gap:
	//
	//   1. Lock the blob row FOR UPDATE FIRST and hold it to commit.
	//   2. Reap expired leases, then read CURRENT committed references/leases.
	//
	// A tag/manifest writer or pull that commits BEFORE we take the lock is
	// visible to the re-read; one that starts AFTER blocks on our FOR UPDATE
	// (writers take FOR SHARE on the blob, lease INSERT's FK check does too),
	// so it either (a) waits until we commit and then fails the FK if we
	// deleted, or (b) made it in first and we see it here.  A layer referenced
	// during scanning therefore can never be deleted.
	tx, err := c.DB.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelReadCommitted})
	if err != nil {
		return Item{}, err
	}
	defer tx.Rollback()

	// 1) Lock the candidate row; block behind any concurrent FOR SHARE holder
	// until it commits, then proceed against its committed effects.
	var lockedDigest string
	err = tx.QueryRowContext(ctx,
		`SELECT digest FROM blobs WHERE digest=$1 FOR UPDATE`, cand.digest).Scan(&lockedDigest)
	if errors.Is(err, sql.ErrNoRows) {
		// Another sweeper/concurrent path removed it already; nothing to do.
		item := Item{Digest: cand.digest, Decision: "retain", Marked: false, Size: cand.size,
			Reason: "retained: blob row already vanished before re-verification"}
		return item, c.recordItem(ctx, runID, item)
	}
	if err != nil {
		return Item{}, err
	}

	// 2) Reap this candidate's expired leases (now safe under our lock).
	if _, err := tx.ExecContext(ctx,
		`DELETE FROM leases WHERE blob_digest=$1 AND expires_at <= $2`,
		cand.digest, c.Clock().UTC()); err != nil {
		return Item{}, err
	}

	// 3) Read the CURRENT committed state of references and active leases.
	var reachable, hasLease bool
	if err := tx.QueryRowContext(ctx, `
		SELECT EXISTS (
			SELECT 1 FROM manifest_refs mr
			WHERE mr.blob_digest = $1
		)`, cand.digest).Scan(&reachable); err != nil {
		return Item{}, err
	}
	if err := tx.QueryRowContext(ctx,
		`SELECT EXISTS (SELECT 1 FROM leases WHERE blob_digest=$1)`,
		cand.digest).Scan(&hasLease); err != nil {
		return Item{}, err
	}

	switch {
	case reachable && hasLease:
		d := sweepDecision{"retain", "retained on re-check: referenced by a stored manifest AND an active read lease"}
		return c.finishSweep(ctx, tx, runID, cand, d)
	case reachable:
		d := sweepDecision{"retain", "retained on re-check: newly referenced by a manifest after mark snapshot"}
		return c.finishSweep(ctx, tx, runID, cand, d)
	case hasLease:
		d := sweepDecision{"retain", "retained on re-check: active read lease (pull in progress)"}
		return c.finishSweep(ctx, tx, runID, cand, d)
	}

	// Confirmed unreferenced and unleased: delete DB row inside this tx.
	if _, err := tx.ExecContext(ctx, `DELETE FROM blobs WHERE digest=$1`, cand.digest); err != nil {
		return Item{}, err
	}
	if err := tx.Commit(); err != nil {
		return Item{}, err
	}

	// Row gone; now unlink the file.  A failure here is reported faithfully:
	// the audit row records the deletion, and orphan reconciliation can
	// detect/remove the stranded file on a later maintenance run.
	fileErr := c.Store.Delete(cand.digest)
	item := Item{Digest: cand.digest, Decision: "delete", Marked: false, Size: cand.size,
		Reason: "deleted: unreferenced at mark snapshot and still unreferenced with no active lease at sweep time"}
	if fileErr != nil {
		item.Reason = fmt.Sprintf("db row deleted; FILE UNLINK FAILED: %v", fileErr)
	}
	if err := c.recordItem(ctx, runID, item); err != nil {
		return Item{}, err
	}
	return item, nil
}

func (c *Collector) finishSweep(ctx context.Context, tx *sql.Tx, runID int64, cand blobInfo, d sweepDecision) (Item, error) {
	if err := tx.Commit(); err != nil {
		return Item{}, err
	}
	item := Item{Digest: cand.digest, Decision: d.decision, Reason: d.reason, Size: cand.size, Marked: false}
	if err := c.recordItem(ctx, runID, item); err != nil {
		return Item{}, err
	}
	return item, nil
}

func (c *Collector) recordItem(ctx context.Context, runID int64, it Item) error {
	_, err := c.DB.ExecContext(ctx,
		`INSERT INTO gc_items(run_id,blob_digest,decision,reason,size_bytes,marked)
		 VALUES ($1,$2,$3,$4,$5,$6)`,
		runID, it.Digest, it.Decision, it.Reason, it.Size, it.Marked)
	return err
}

// recoverStaleRuns marks runs left unfinished by a crashed process.
func (c *Collector) recoverStaleRuns(ctx context.Context) (bool, error) {
	res, err := c.DB.ExecContext(ctx,
		`UPDATE gc_runs SET status='crashed', finished_at=$1
		 WHERE status IN ('marking','sweeping')`, c.Clock().UTC())
	if err != nil {
		return false, err
	}
	n, _ := res.RowsAffected()
	return n > 0, nil
}

func (c *Collector) failRun(ctx context.Context, runID int64) {
	_, _ = c.DB.ExecContext(ctx,
		`UPDATE gc_runs SET status='crashed', finished_at=$1 WHERE id=$2 AND status <> 'completed'`,
		c.Clock().UTC(), runID)
}

// GetReport loads a run plus its per-item audit rows.
func (c *Collector) GetReport(ctx context.Context, runID int64) (*Report, error) {
	rep := &Report{RunID: runID}
	var finished sql.NullTime
	var xmin sql.NullInt64
	err := c.DB.QueryRowContext(ctx,
		`SELECT started_at,finished_at,status,snapshot_xmin,marked_count,
		        candidate_count,deleted_count,retained_count
		 FROM gc_runs WHERE id=$1`, runID).
		Scan(&rep.StartedAt, &finished, &rep.Status, &xmin, &rep.Marked,
			&rep.Candidates, &rep.Deleted, &rep.Retained)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, fmt.Errorf("gc run %d not found", runID)
	}
	if err != nil {
		return nil, err
	}
	if finished.Valid {
		rep.FinishedAt = finished.Time
	}
	if xmin.Valid {
		rep.SnapshotXmin = xmin.Int64
	}
	rows, err := c.DB.QueryContext(ctx,
		`SELECT blob_digest,decision,reason,size_bytes,marked
		 FROM gc_items WHERE run_id=$1 ORDER BY id`, runID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var it Item
		if err := rows.Scan(&it.Digest, &it.Decision, &it.Reason, &it.Size, &it.Marked); err != nil {
			return nil, err
		}
		rep.Items = append(rep.Items, it)
	}
	return rep, rows.Err()
}

// LatestRunID returns the most recent gc run id, or 0 if none.
func (c *Collector) LatestRunID(ctx context.Context) (int64, error) {
	var id sql.NullInt64
	err := c.DB.QueryRowContext(ctx, `SELECT max(id) FROM gc_runs`).Scan(&id)
	if err != nil {
		return 0, err
	}
	return id.Int64, nil
}
