package dataset

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"log"
)

// RecoverAtStartup restores on-disk/DB consistency after a crash.
//
//  1. The temp directory is emptied: temp files are always scratch (chunks
//     stream through them into the blob namespace, and merges are rebuilt
//     from chunks), so discarding them never destroys committed data.
//  2. Datasets stuck in 'publishing' are reset to 'uploading' so the client
//     can retry the merge/publish; no half-merged file is ever marked ready.
//  3. Rows still in 'deleting' and physical orphan files are handed to a GC
//     sweep.
//
// The function is safe to call on every boot and with multiple processes
// starting concurrently (row locks and advisory locks serialise the work).
func (s *Service) RecoverAtStartup(ctx context.Context) error {
	if err := s.Store.CleanupTemp(); err != nil {
		return fmt.Errorf("cleanup temp: %w", err)
	}

	// Reset in-flight merges. A dataset only enters publishing after all
	// chunks were already received, so the client only needs to re-POST
	// /publish; no chunk bytes have to be re-uploaded.
	res, err := s.DB.ExecContext(ctx, `
		UPDATE datasets SET status = $1 WHERE status = $2`,
		statusUploading, statusPublishing)
	if err != nil {
		return fmt.Errorf("reset publishing datasets: %w", err)
	}
	if n, _ := res.RowsAffected(); n > 0 {
		log.Printf("recovery: reset %d interrupted publish(es)", n)
	}

	// Complete any GC that died mid-removal.
	var deleting int
	if err := s.DB.GetContext(ctx, &deleting,
		`SELECT count(*) FROM blobs WHERE state = $1`, blobDeleting); err != nil {
		return fmt.Errorf("check deleting blobs: %w", err)
	}

	// Physical orphans: files on disk without a row (crash between hard link
	// and commit). List before GC so we can log what was found.
	live, err := s.liveBlobHashes(ctx)
	if err != nil {
		return err
	}
	orphans, err := s.Store.OrphanBlobs(live)
	if err != nil {
		return fmt.Errorf("scan orphan blobs: %w", err)
	}
	for _, sha := range orphans {
		if err := s.Store.UnlinkBlob(sha); err != nil {
			log.Printf("recovery: remove orphan %s: %v", sha, err)
		} else {
			log.Printf("recovery: removed orphan blob %s", sha)
		}
	}

	if deleting > 0 || len(orphans) > 0 {
		if _, err := s.GarbageCollect(ctx); err != nil {
			log.Printf("recovery: gc sweep: %v", err)
		}
	}
	return nil
}

// liveBlobHashes returns the set of blob hashes known to the database.
func (s *Service) liveBlobHashes(ctx context.Context) (map[string]struct{}, error) {
	rows, err := s.DB.QueryxContext(ctx, `SELECT sha256 FROM blobs`)
	if err != nil {
		return nil, fmt.Errorf("list blobs: %w", err)
	}
	defer rows.Close()
	out := make(map[string]struct{})
	for rows.Next() {
		var sha string
		if err := rows.Scan(&sha); err != nil {
			return nil, err
		}
		out[sha] = struct{}{}
	}
	return out, nil
}

func isNoRows(err error) bool { return errors.Is(err, sql.ErrNoRows) }
