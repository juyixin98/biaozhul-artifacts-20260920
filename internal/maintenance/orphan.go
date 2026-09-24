// Package maintenance handles operational cleanup that is deliberately
// separate from layer garbage collection:
//
//   - orphan staged uploads: temp files whose bookkeeping row is gone,
//     or rows whose file is gone;
//   - stale upload rows: long-running incomplete uploads;
//   - files present in the CAS with no blob row (crash between DB commit and
//     publish) and blob rows whose file is missing.
//
// Every decision gets its own audit table so the reasons are never conflated
// with "why was a referenced layer deleted" (it never should be).
package maintenance

import (
	"context"
	"database/sql"
	"fmt"
	"os"
	"time"

	"layer-gc/internal/storage"
)

type Item struct {
	Path    string
	Reason  string
	Size    int64
	Removed bool
}

type Report struct {
	RunID          int64
	StartedAt      time.Time
	FinishedAt     time.Time
	Removed        int
	ReclaimedBytes int64
	Items          []Item
}

type Cleaner struct {
	DB       *sql.DB
	Store    *storage.Store
	Clock    func() time.Time
	StaleAge time.Duration // incomplete uploads older than this are orphans
}

func NewCleaner(db *sql.DB, store *storage.Store) *Cleaner {
	return &Cleaner{DB: db, Store: store, Clock: time.Now, StaleAge: 24 * time.Hour}
}

// CleanOrphans reconciles staged temp files with blob_uploads and optionally
// removes them. dryRun=false performs deletion and writes removed=true rows.
func (c *Cleaner) CleanOrphans(ctx context.Context, dryRun bool) (*Report, error) {
	started := c.Clock().UTC()
	var runID int64
	if err := c.DB.QueryRowContext(ctx,
		`INSERT INTO orphan_cleanup_runs(started_at) VALUES ($1) RETURNING id`,
		started).Scan(&runID); err != nil {
		return nil, err
	}
	rep := &Report{RunID: runID, StartedAt: started}

	record := func(path, reason string, size int64, removed bool) {
		_, _ = c.DB.ExecContext(ctx,
			`INSERT INTO orphan_cleanup_items(run_id,path,reason,size_bytes,removed)
			 VALUES ($1,$2,$3,$4,$5)`, runID, path, reason, size, removed)
		rep.Items = append(rep.Items, Item{Path: path, Reason: reason, Size: size, Removed: removed})
		if removed {
			rep.Removed++
			rep.ReclaimedBytes += size
		}
	}

	// 1) Temp files vs upload bookkeeping.
	files, err := c.Store.StagedFiles()
	if err != nil {
		return nil, err
	}
	for _, p := range files {
		fi, err := os.Stat(p)
		if err != nil {
			record(p, fmt.Sprintf("orphan temp: stat failed: %v", err), 0, false)
			continue
		}
		name := baseName(p)
		var completed bool
		var startedAt time.Time
		err = c.DB.QueryRowContext(ctx,
			`SELECT completed, started_at FROM blob_uploads WHERE upload_id=$1`, name).
			Scan(&completed, &startedAt)
		switch {
		case err == sql.ErrNoRows:
			removed := false
			if !dryRun {
				if rmErr := os.Remove(p); rmErr == nil {
					removed = true
				}
			}
			record(p, "orphan temp: no blob_uploads row (abandoned/crashed upload)", fi.Size(), removed)
		case err != nil:
			return nil, err
		case completed:
			// Commit completed but publish/rename did not finish: orphan file.
			removed := false
			if !dryRun {
				if rmErr := os.Remove(p); rmErr == nil {
					removed = true
				}
				_, _ = c.DB.ExecContext(ctx, `DELETE FROM blob_uploads WHERE upload_id=$1`, name)
			}
			record(p, "orphan temp: upload marked completed but never published (crash after commit)", fi.Size(), removed)
		case c.Clock().Sub(startedAt) > c.StaleAge:
			removed := false
			if !dryRun {
				if rmErr := os.Remove(p); rmErr == nil {
					removed = true
				}
				_, _ = c.DB.ExecContext(ctx, `DELETE FROM blob_uploads WHERE upload_id=$1`, name)
			}
			record(p, fmt.Sprintf("stale temp: incomplete upload older than %s", c.StaleAge), fi.Size(), removed)
		default:
			record(p, "retained: active upload in progress", fi.Size(), false)
		}
	}

	// 2) Upload rows whose temp file vanished — both completed (crash after
	// DB commit, before publish) and incomplete (crash/abort mid-upload).
	rows, err := c.DB.QueryContext(ctx,
		`SELECT upload_id, temp_path, completed FROM blob_uploads`)
	if err != nil {
		return nil, err
	}
	type up struct {
		id, path  string
		completed bool
	}
	var uploads []up
	for rows.Next() {
		var u up
		if err := rows.Scan(&u.id, &u.path, &u.completed); err != nil {
			rows.Close()
			return nil, err
		}
		uploads = append(uploads, u)
	}
	rows.Close()
	for _, u := range uploads {
		if _, err := os.Stat(u.path); os.IsNotExist(err) {
			reason := "dangling bookkeeping: temp file missing (crash/abort before cleanup)"
			if u.completed {
				reason = "dangling bookkeeping: upload committed but temp file gone (crash between commit and publish)"
			}
			// No file exists -> removed=false; only bookkeeping is dropped.
			record(u.path, reason, 0, false)
			if !dryRun {
				_, _ = c.DB.ExecContext(ctx, `DELETE FROM blob_uploads WHERE upload_id=$1`, u.id)
			}
		}
	}

	// 3) CAS files vs blobs table (both directions, report-only).
	diskBlobs, err := c.Store.PublishedBlobs()
	if err != nil {
		return nil, err
	}
	diskSet := map[string]bool{}
	for _, d := range diskBlobs {
		diskSet[d] = true
	}
	dbRows, err := c.DB.QueryContext(ctx, `SELECT digest FROM blobs`)
	if err != nil {
		return nil, err
	}
	dbSet := map[string]bool{}
	for dbRows.Next() {
		var d string
		if err := dbRows.Scan(&d); err != nil {
			dbRows.Close()
			return nil, err
		}
		dbSet[d] = true
	}
	dbRows.Close()
	for d := range diskSet {
		if !dbSet[d] {
			record("cas:"+d, "orphan CAS file: present on disk, absent from blobs table (crash before row commit or external write)", 0, false)
		}
	}
	for d := range dbSet {
		if !diskSet[d] {
			record("cas:"+d, "MISSING backing file: blob row exists but file absent (crash between commit and publish)", 0, false)
		}
	}

	finished := c.Clock().UTC()
	rep.FinishedAt = finished
	if _, err := c.DB.ExecContext(ctx,
		`UPDATE orphan_cleanup_runs SET finished_at=$1,removed_count=$2,reclaimed_bytes=$3 WHERE id=$4`,
		finished, rep.Removed, rep.ReclaimedBytes, runID); err != nil {
		return nil, err
	}
	return rep, nil
}

func baseName(p string) string {
	for i := len(p) - 1; i >= 0; i-- {
		if p[i] == '/' {
			return p[i+1:]
		}
	}
	return p
}
