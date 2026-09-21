package store

import (
	"log"
	"os"
	"path/filepath"

	"github.com/jmoiron/sqlx"
)

// Recover reconciles the file store with the database after a crash. It runs
// once at startup before any request is served, so it needs no advisory locks:
// no other process is modifying the directories.
//
// Crash points and their outcome:
//
//	staging rename -> objects/ ... row insert commits
//	    normal path, nothing to do.
//	staging rename -> objects/ ... row insert rolls back / process dies
//	    committed file without a row -> orphan, deleted.
//	refcount reaches zero, blob moved to graveyard, row deleted, tx commits ...
//	    crash before the caller unlinks the graveyard copy -> purged here.
//	refcount reaches zero, blob moved to graveyard, tx rolls back
//	    impossible state is not produced: the rename is inside the same tx and a
//	    rollback returns the refcount; the graveyard copy is then stale and is
//	    purged here (its digest still has a row, the committed object was moved
//	    though — see RestoreGraveyard note).
func Recover(db *sqlx.DB, fs *FileStore) error {
	// First restore any graveyard entries whose rows still exist (a quarantine
	// whose transaction rolled back). Move them back to the object path.
	if err := restoreGraveyard(db, fs); err != nil {
		return err
	}
	if err := fs.PurgeGraveyard(); err != nil {
		return err
	}
	if err := fs.PurgeStaging(); err != nil {
		return err
	}

	known := func(digest string) (bool, error) {
		var exists bool
		if err := db.Get(&exists, `SELECT EXISTS(SELECT 1 FROM objects WHERE digest = $1)`, digest); err != nil {
			return false, err
		}
		return exists, nil
	}
	orphans, err := fs.OrphanObjects(known)
	if err != nil {
		return err
	}
	for _, d := range orphans {
		if err := fs.RemoveObject(d); err != nil && !os.IsNotExist(err) {
			return err
		}
		log.Printf("recover: removed orphan object %s", d)
	}
	return nil
}

// restoreGraveyard moves a quarantined blob back when its row still exists,
// which means the transaction that quarantined it rolled back.
func restoreGraveyard(db *sqlx.DB, fs *FileStore) error {
	entries, err := os.ReadDir(fs.graveyard)
	if err != nil {
		return err
	}
	for _, e := range entries {
		name := e.Name()
		// Graveyard names are "<64-hex-digest>-<suffix>".
		if len(name) < 65 {
			continue
		}
		digest := name[:64]
		var exists bool
		if err := db.Get(&exists, `SELECT EXISTS(SELECT 1 FROM objects WHERE digest = $1)`, digest); err != nil {
			return err
		}
		if !exists {
			continue // row gone for good; PurgeGraveyard will unlink it
		}
		src := filepath.Join(fs.graveyard, name)
		dst := fs.ObjectPath(digest)
		if _, err := os.Stat(dst); err == nil {
			continue // a fresh copy already committed
		}
		if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
			return err
		}
		if err := os.Rename(src, dst); err != nil {
			return err
		}
		log.Printf("recover: restored quarantined object %s after rollback", digest)
	}
	return nil
}
