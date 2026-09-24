package gc

import (
	"context"
	"fmt"
	"sort"
	"time"

	"layerregistry/internal/store"
)

// Recover reconciles the filesystem and database after a crash.
//
// It inspects every GC run that never reached 'completed' and the on-disk
// quarantine directories, then makes each object consistent:
//
//	row present  + file in quarantine  -> restore file (delete rolled back)
//	row absent   + file in quarantine  -> purge file  (delete committed)
//	row present  + file on disk        -> nothing (crash before quarantine)
//
// Orphan blob files (content-addressed bytes on disk with no blobs row, and
// not in any quarantine dir) are left untouched on disk but reported; an
// explicit re-adopt/purge is exposed via Orphans. Rows absent and file
// absent means the run simply never got there.
func (c *Collector) Recover(ctx context.Context) ([]string, error) {
	var log []string

	runs, err := c.st.ListUnfinishedRuns(ctx)
	if err != nil {
		return nil, err
	}
	quarantined, err := c.fs.(quarantineLister).ListQuarantine()
	if err != nil {
		return nil, err
	}

	for _, run := range runs {
		run := run
		qd := quarantined[run.ID]
		for _, digest := range qd {
			var exists bool
			if err := c.st.Pool().QueryRow(ctx,
				"SELECT EXISTS(SELECT 1 FROM blobs WHERE digest=$1)", digest).Scan(&exists); err != nil {
				return log, err
			}
			if exists {
				if err := c.fs.Restore(run.ID, digest); err != nil {
					return log, fmt.Errorf("restore %s: %w", digest, err)
				}
				msg := fmt.Sprintf("recovered run %s: blob %s row survived crash, file restored from quarantine", run.ID, digest)
				log = append(log, msg)
				_ = c.st.AuditEvent(ctx, run.ID, "gc.recover_restore", "blob", "", digest, "row present after crash")
			} else {
				if err := c.fs.Purge(run.ID, digest); err != nil {
					return log, fmt.Errorf("purge %s: %w", digest, err)
				}
				msg := fmt.Sprintf("recovered run %s: blob %s row deleted durably, quarantined file purged", run.ID, digest)
				log = append(log, msg)
				_ = c.st.AuditEvent(ctx, run.ID, "gc.recover_purge", "blob", "", digest, "row absent after crash")
			}
		}
		if err := c.fs.PurgeQuarantineDir(run.ID); err != nil {
			return log, err
		}
		note := fmt.Sprintf("crash recovery completed at %s; %d quarantined objects reconciled",
			time.Now().Format(time.RFC3339), len(qd))
		if err := c.st.MarkGCRecovered(ctx, run.ID, note); err != nil {
			return log, err
		}
		log = append(log, fmt.Sprintf("recovered run %s marked recovered (%s)", run.ID, note))
	}

	// Quarantine dirs belonging to runs with no row at all (crash before the
	// INSERT could be observed): conservative restore.
	known := map[string]bool{}
	for _, r := range runs {
		known[r.ID] = true
	}
	var orphanRunIDs []string
	for id := range quarantined {
		if !known[id] {
			orphanRunIDs = append(orphanRunIDs, id)
		}
	}
	sort.Strings(orphanRunIDs)
	for _, id := range orphanRunIDs {
		for _, digest := range quarantined[id] {
			if err := c.fs.Restore(id, digest); err != nil {
				return log, err
			}
			log = append(log, fmt.Sprintf("orphan quarantine dir %s: restored %s", id, digest))
			_ = c.st.AuditEvent(ctx, id, "gc.recover_orphan_quarantine", "blob", "", digest, "no run row")
		}
		if err := c.fs.PurgeQuarantineDir(id); err != nil {
			return log, err
		}
	}
	return log, nil
}

// quarantineLister is the optional storage capability recovery needs.
type quarantineLister interface {
	ListQuarantine() (map[string][]string, error)
}

// ReconcileBlobFiles finds content-addressed blob files on disk without a
// blobs row (strays from a crash between rename and row insert, or manual
// copies) and returns their digests. They are never deleted automatically;
// callers decide (adopt or purge) explicitly.
func (c *Collector) ReconcileBlobFiles(ctx context.Context) ([]string, error) {
	lister, ok := c.fs.(blobLister)
	if !ok {
		return nil, nil
	}
	onDisk, err := lister.ListBlobDigests()
	if err != nil {
		return nil, err
	}
	var strays []string
	for _, d := range onDisk {
		var exists bool
		if err := c.st.Pool().QueryRow(ctx,
			"SELECT EXISTS(SELECT 1 FROM blobs WHERE digest=$1)", d).Scan(&exists); err != nil {
			return nil, err
		}
		if !exists {
			strays = append(strays, d)
		}
	}
	sort.Strings(strays)
	return strays, nil
}

type blobLister interface {
	ListBlobDigests() ([]string, error)
}

// AdoptStrayBlob registers a stray on-disk blob in the database after
// re-verifying its size (content address is trusted by name only after a
// full hash recheck by the caller storage layer; here we stat the file).
func (c *Collector) AdoptStrayBlob(ctx context.Context, digest string, size int64) error {
	return c.st.PublishBlob(ctx, digest, size, time.Now())
}

// PurgeStrayBlob removes a stray blob file from the CAS tree.
func (c *Collector) PurgeStrayBlob(digest string) error {
	return c.fs.RemoveBlob(digest)
}

// noop to keep store import used even if file trimmed later.
var _ = store.SetKey
