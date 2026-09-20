package app

import (
	"context"
	"database/sql"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"os"

	"github.com/jmoiron/sqlx"
)

// Content-addressing and reference counting.
//
// Every published dataset points at a files row keyed by the content digest;
// identical content shares one row and one on-disk file. A file may only be
// deleted once no dataset references it. The danger is the race between a
// publisher creating a new reference to digest D and a garbage collector
// deleting D's last reference:
//
//	publisher: merge done, no files row yet
//	collector: refs==0, delete row, unlink file
//	publisher: create row ... but the file is gone
//
// Both sides take the same transaction-scoped advisory lock derived from D,
// and the publisher promotes its private tmp file into place while holding
// it. The collector re-checks row existence under the same lock before
// unlinking, so a file referenced by a committed row is never removed.

// fileLockKey derives a stable bigint advisory-lock key from a digest.
func fileLockKey(sha256Hex string) int64 {
	b, err := hex.DecodeString(sha256Hex[:16])
	if err != nil {
		return 0
	}
	return int64(binary.BigEndian.Uint64(b))
}

// addFileRef registers a reference to the content-addressed file digest.
// On the first reference it promotes tmpPath into the store and inserts the
// files row; subsequent references discard the duplicate tmp copy and reuse
// the existing row. Must run inside the publisher's transaction.
func addFileRef(ctx context.Context, fs *FileStore, tx *sqlx.Tx, sha256Hex string, size int64, tmpPath string) (int64, error) {
	if _, err := tx.ExecContext(ctx, `SELECT pg_advisory_xact_lock($1)`, fileLockKey(sha256Hex)); err != nil {
		return 0, err
	}

	var id int64
	err := tx.GetContext(ctx, &id, `SELECT id FROM files WHERE sha256=$1`, sha256Hex)
	switch {
	case err == nil:
		// Identical content already stored; the tmp merge copy is redundant.
		if rmErr := os.Remove(tmpPath); rmErr != nil && !errors.Is(rmErr, os.ErrNotExist) {
			return 0, rmErr
		}
		return id, nil
	case !errors.Is(err, sql.ErrNoRows):
		return 0, err
	}

	if err := fs.PromoteMerged(tmpPath, sha256Hex); err != nil {
		return 0, err
	}
	if err := tx.GetContext(ctx, &id,
		`INSERT INTO files (sha256, size, path) VALUES ($1,$2,$3) RETURNING id`,
		sha256Hex, size, fs.FilePath(sha256Hex)); err != nil {
		return 0, err
	}
	return id, nil
}

// releaseFileRef deletes the files row once no dataset references it. It
// returns the digest whose on-disk file should be garbage-collected by the
// caller after commit, or "" while the file is still shared.
func releaseFileRef(ctx context.Context, tx *sqlx.Tx, fileID int64) (string, error) {
	var f struct {
		SHA256 string `db:"sha256"`
	}
	if err := tx.GetContext(ctx, &f, `SELECT sha256 FROM files WHERE id=$1`, fileID); err != nil {
		return "", err
	}
	if _, err := tx.ExecContext(ctx, `SELECT pg_advisory_xact_lock($1)`, fileLockKey(f.SHA256)); err != nil {
		return "", err
	}

	var refs int
	if err := tx.GetContext(ctx, &refs,
		`SELECT count(*) FROM datasets WHERE file_id=$1`, fileID); err != nil {
		return "", err
	}
	if refs > 0 {
		return "", nil
	}
	if _, err := tx.ExecContext(ctx, `DELETE FROM files WHERE id=$1`, fileID); err != nil {
		return "", err
	}
	return f.SHA256, nil
}

// GCOrphanFile unlinks the on-disk file for digest only if no files row
// exists for it. Runs in its own short transaction under the per-digest
// advisory lock, serializing it against a concurrent publisher. If a
// publisher committed first the row exists and the file is kept; otherwise
// the file is removed and any later publisher recreates it from its private
// tmp merge output once it acquires the lock.
func GCOrphanFile(ctx context.Context, db *sqlx.DB, fs *FileStore, sha256Hex string) error {
	tx, err := db.BeginTxx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()

	if _, err := tx.ExecContext(ctx, `SELECT pg_advisory_xact_lock($1)`, fileLockKey(sha256Hex)); err != nil {
		return err
	}
	var exists bool
	if err := tx.GetContext(ctx, &exists,
		`SELECT EXISTS(SELECT 1 FROM files WHERE sha256=$1)`, sha256Hex); err != nil {
		return err
	}
	if !exists {
		if err := os.Remove(fs.FilePath(sha256Hex)); err != nil && !errors.Is(err, os.ErrNotExist) {
			return err
		}
	}
	return tx.Commit()
}
