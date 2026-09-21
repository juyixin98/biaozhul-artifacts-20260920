package store

import (
	"database/sql"
	"errors"
	"fmt"
	"hash/fnv"

	"github.com/jmoiron/sqlx"
)

// ErrNotFound indicates an object row does not exist.
var ErrNotFound = errors.New("object not found")

// ObjectService is the refcount gate in front of FileStore. Every path that
// touches a committed blob goes through it. Per-digest PostgreSQL advisory
// locks serialize "get-or-create" against "release-to-zero", and the physical
// rename into the graveyard happens inside the lock so a concurrent Acquire can
// never find its file missing after the refcount flip.
type ObjectService struct {
	db *sqlx.DB
	fs *FileStore

	// FaultAfterCommitStaging, if set, fires in Acquire after the staging file
	// has been renamed to its committed path but before the row is inserted.
	// Returning an error simulates a process crash in that window, leaving an
	// orphan file for startup recovery to remove. Test-only.
	FaultAfterCommitStaging func(digest string) error
}

func NewObjectService(db *sqlx.DB, fs *FileStore) *ObjectService {
	return &ObjectService{db: db, fs: fs}
}

// lockKey maps a digest to a 64-bit advisory-lock key: a fixed namespace
// prefix in the high 32 bits and an FNV hash in the low 32 bits.
func lockKey(digest string) int64 {
	h := fnv.New32a()
	_, _ = h.Write([]byte(digest))
	v := (uint64(0x53594e31) << 32) | uint64(h.Sum32()) // "SYN1" namespace
	return int64(v)
}

// txLock acquires the transaction-scoped per-digest advisory lock.
func txLock(tx *sqlx.Tx, digest string) error {
	_, err := tx.Exec(`SELECT pg_advisory_xact_lock($1)`, lockKey(digest))
	return err
}

// Acquire adds one reference to the object identified by digest. If the object
// already exists (present or freshly un-deleted garbage), only the refcount is
// bumped. Otherwise the bytes must be provided as a local staging file: they
// are committed to the content-addressed path and a new row inserted.
//
// Must be called with the caller already inside tx. The physical rename is
// ordered before the row insert, so a crash in between leaves an orphan file
// (cleaned at startup) rather than a row without a file.
func (s *ObjectService) Acquire(tx *sqlx.Tx, digest string, size int64, stagingPath string) (int64, error) {
	if err := txLock(tx, digest); err != nil {
		return 0, err
	}

	var id int64
	err := tx.Get(&id, `SELECT id FROM objects WHERE digest = $1`, digest)
	if err == nil {
		if _, err := tx.Exec(
			`UPDATE objects SET refcount = refcount + 1, status = 'present' WHERE id = $1`, id); err != nil {
			return 0, err
		}
		return id, nil
	}
	if !errors.Is(err, sql.ErrNoRows) {
		return 0, err
	}

	if stagingPath == "" {
		return 0, fmt.Errorf("internal: staging path required for new object %s", digest)
	}
	if err := s.fs.CommitStaging(stagingPath, digest, size); err != nil {
		return 0, err
	}
	if s.FaultAfterCommitStaging != nil {
		if ferr := s.FaultAfterCommitStaging(digest); ferr != nil {
			return 0, ferr
		}
	}
	if err := tx.Get(&id,
		`INSERT INTO objects(digest, size_bytes, refcount, status)
		 VALUES ($1, $2, 1, 'present') RETURNING id`, digest, size); err != nil {
		return 0, err
	}
	return id, nil
}

// ReleaseLocked drops one reference. When the refcount reaches zero the row is
// deleted and the blob is moved to the graveyard while still holding the
// advisory lock. The returned graveyard path is non-empty exactly when a file
// was quarantined; the caller must unlink it only AFTER the outer transaction
// commits, otherwise a rollback would orphan the bytes.
//
// Must be called inside tx.
func (s *ObjectService) ReleaseLocked(tx *sqlx.Tx, digest string) (string, error) {
	if err := txLock(tx, digest); err != nil {
		return "", err
	}
	var id int64
	switch err := tx.Get(&id, `SELECT id FROM objects WHERE digest = $1`, digest); {
	case err == nil:
	case errors.Is(err, sql.ErrNoRows):
		return "", ErrNotFound
	default:
		return "", err
	}
	var rc int32
	if err := tx.Get(&rc, `UPDATE objects SET refcount = refcount - 1 WHERE id = $1 RETURNING refcount`, id); err != nil {
		return "", err
	}
	if rc > 0 {
		return "", nil
	}
	if _, err := tx.Exec(`DELETE FROM objects WHERE id = $1 AND refcount = 0`, id); err != nil {
		return "", err
	}
	_, graveyardPath, err := s.fs.Quarantine(digest)
	if err != nil {
		return "", err
	}
	return graveyardPath, nil
}

// ReleaseManyLocked drops one reference to every distinct digest. Returns the
// graveyard paths created; the caller unlinks them after the outer transaction
// commits.
func (s *ObjectService) ReleaseManyLocked(tx *sqlx.Tx, digests []string) ([]string, error) {
	var paths []string
	seen := make(map[string]struct{}, len(digests))
	for _, d := range digests {
		if _, ok := seen[d]; ok {
			continue
		}
		seen[d] = struct{}{}
		p, err := s.ReleaseLocked(tx, d)
		if err != nil {
			return nil, err
		}
		if p != "" {
			paths = append(paths, p)
		}
	}
	return paths, nil
}
