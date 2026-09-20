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

// fileLockKey derives a postgres advisory-lock key from a content digest, so
// all add-reference / release-reference / GC operations for the same content
// serialize on one lock.
func fileLockKey(sha256Hex string) int64 {
	b, err := hex.DecodeString(sha256Hex[:16])
	if err != nil {
		return 0
	}
	return int64(binary.BigEndian.Uint64(b))
}

// addFileRef registers a reference to the content-addressed file with the
// given digest, promoting tmpPath into the store if this is the first
// reference. The advisory lock serializes against concurrent releaseFileRef /
// GCOrphanFile calls for the same digest, so a file can never be deleted
// while a new reference to it is being created.
func addFileRef(ctx context.Context, fs *FileStore, tx *sqlx.Tx, sha256Hex string, size int64, tmpPath string) (int64, error) {
	if _, err := tx.ExecContext(ctx, `SELECT pg_advisory_xact_lock($1)`, fileLockKey(sha256Hex)); err != nil {
		return 0, err
	}
	var id int64
	err := tx.GetContext(ctx, &id, `SELECT id FROM files WHERE sha256=$1`, sha256Hex)
	if err == nil {
		os.Remove(tmpPath) // identical content already stored
		return id, nil
	}
	if !errors.Is(err, sql.ErrNoRows) {
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

// releaseFileRef drops the files row when no dataset references it anymore.
// It returns the digest whose on-disk file should be garbage-collected after
// the caller commits (via GCOrphanFile), or "" if the file is still shared.
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
	if err := tx.GetContext(ctx, &refs, `SELECT count(*) FROM datasets WHERE file_id=$1`, fileID); err != nil {
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

// GCOrphanFile deletes the on-disk content file if no files row exists for
// the digest. The advisory lock serializes against a concurrent publisher
// re-referencing the same content: either the publisher committed first (row
// exists, file kept) or it hasn't (file removed; the publisher re-creates it
// from its private tmp file when it later acquires the lock).
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
	if err := tx.GetContext(ctx, &exists, `SELECT EXISTS(SELECT 1 FROM files WHERE sha256=$1)`, sha256Hex); err != nil {
		return err
	}
	if !exists {
		if err := os.Remove(fs.FilePath(sha256Hex)); err != nil && !errors.Is(err, os.ErrNotExist) {
			return err
		}
	}
	return tx.Commit()
}
