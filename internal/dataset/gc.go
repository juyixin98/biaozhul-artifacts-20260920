package dataset

import (
	"context"
	"fmt"
	"log"
	"time"
)

// blobLockKey is the 64-bit key for the advisory lock that serialises blob
// adoption against blob removal. Every publisher holds it for the short
// transaction that hard-links a file, adopts its row and inserts a reference;
// every garbage collector holds it for the transaction that removes a blob.
// Mutual exclusion is what guarantees a committed reference always points at
// a present file:
//
//   - publisher holding the lock: GC waits, then sees refcount > 0 and skips;
//   - GC holding the lock: it deletes the row and unlinks the file entirely
//     before the publisher proceeds; the publisher's hard link then recreates
//     the missing path from its still-present temp upload and inserts a new
//     row.
const blobLockKey int64 = 7123659081

// GarbageCollect removes unreferenced blob files. See blobLockKey for the
// race-safety argument. Rows already in 'deleting' (left by an interrupted
// GC run) are completed first.
func (s *Service) GarbageCollect(ctx context.Context) (int, error) {
	pending, err := s.listDeleting(ctx)
	if err != nil {
		return 0, err
	}
	fresh, err := s.markDeletingBatch(ctx)
	if err != nil {
		return 0, err
	}
	pending = append(pending, fresh...)

	removed := 0
	for _, sha := range pending {
		done, err := s.finishDeleting(ctx, sha)
		if err != nil {
			log.Printf("gc: could not remove blob %s: %v", sha, err)
			continue
		}
		if done {
			removed++
		}
	}
	return removed, nil
}

func (s *Service) listDeleting(ctx context.Context) ([]string, error) {
	var shas []string
	if err := s.DB.SelectContext(ctx, &shas,
		`SELECT sha256 FROM blobs WHERE state = $1 ORDER BY sha256`, blobDeleting); err != nil {
		return nil, fmt.Errorf("gc list deleting: %w", err)
	}
	return shas, nil
}

// markDeletingBatch locks up to 100 unreferenced ready blobs and moves them
// to 'deleting' in one transaction. SKIP LOCKED lets concurrent GC passes
// (periodic run overlapping a delete-triggered run) coexist.
func (s *Service) markDeletingBatch(ctx context.Context) ([]string, error) {
	tx, err := s.DB.BeginTxx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer func() { _ = tx.Rollback() }()

	var shas []string
	if err := tx.SelectContext(ctx, &shas, `
		SELECT sha256 FROM blobs
		 WHERE state = $1 AND refcount = 0
		 ORDER BY sha256
		 LIMIT 100
		 FOR UPDATE SKIP LOCKED`, blobReady); err != nil {
		return nil, fmt.Errorf("gc select: %w", err)
	}
	for _, sha := range shas {
		if _, err := tx.ExecContext(ctx,
			`UPDATE blobs SET state = $1 WHERE sha256 = $2 AND state = $3 AND refcount = 0`,
			blobDeleting, sha, blobReady); err != nil {
			return nil, err
		}
	}
	if err := tx.Commit(); err != nil {
		return nil, err
	}
	return shas, nil
}

// finishDeleting removes one 'deleting' blob while holding the blob advisory
// lock. The file is unlinked inside the open transaction, so a rollback on
// error leaves the row in place. Rows that regain references (only possible
// for crash leftovers in defensive code paths) are restored to ready.
func (s *Service) finishDeleting(ctx context.Context, sha string) (bool, error) {
	tx, err := s.DB.BeginTxx(ctx, nil)
	if err != nil {
		return false, err
	}
	defer func() { _ = tx.Rollback() }()

	if _, err := tx.ExecContext(ctx, `SELECT pg_advisory_xact_lock($1)`, blobLockKey); err != nil {
		return false, fmt.Errorf("acquire blob lock: %w", err)
	}

	var refcount int
	err = tx.GetContext(ctx, &refcount,
		`SELECT refcount FROM blobs WHERE sha256 = $1 FOR UPDATE`, sha)
	if err != nil {
		if isNoRows(err) {
			return false, tx.Commit() // another GC pass finished it
		}
		return false, fmt.Errorf("re-lock blob: %w", err)
	}
	if refcount > 0 {
		// Defensive: only reachable for rows marked deleting long ago. Put
		// them back into service; the publisher path never adopts a
		// 'deleting' row, so this should not be needed in steady state.
		if _, err := tx.ExecContext(ctx,
			`UPDATE blobs SET state = $1 WHERE sha256 = $2`, blobReady, sha); err != nil {
			return false, err
		}
		return false, tx.Commit()
	}
	if _, err := tx.ExecContext(ctx, `DELETE FROM blobs WHERE sha256 = $1`, sha); err != nil {
		return false, fmt.Errorf("delete blob row: %w", err)
	}
	// Unlink while the transaction and advisory lock are still held: either
	// the file and row vanish together, or the rollback brings the row back.
	if err := s.Store.UnlinkBlob(sha); err != nil {
		return false, fmt.Errorf("unlink blob file: %w", err)
	}
	return true, tx.Commit()
}

// GCInterval is how long the background GC loop sleeps between sweeps.
const GCInterval = 30 * time.Second

// RunGC loops GarbageCollect until ctx is cancelled.
func (s *Service) RunGC(ctx context.Context) {
	t := time.NewTicker(GCInterval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			if n, err := s.GarbageCollect(ctx); err != nil {
				log.Printf("gc sweep error: %v", err)
			} else if n > 0 {
				log.Printf("gc: removed %d unreferenced blob(s)", n)
			}
		}
	}
}
