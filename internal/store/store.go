// Package store keeps cache metadata in SQLite. It enforces the
// single-publisher rule: for any cache key at most one client may hold
// the "building" lease; everyone else waits for the outcome. Failed
// builds are recorded as failures and are never served as cache hits.
package store

import (
	"context"
	"database/sql"
	"fmt"
	"time"

	_ "modernc.org/sqlite"
)

const (
	StatusBuilding    = "building"
	StatusSuccess     = "success"
	StatusFailed      = "failed"
	StatusQuarantined = "quarantined"
)

type Entry struct {
	Key            string `json:"key"`
	Status         string `json:"status"`
	Owner          string `json:"owner,omitempty"`
	RequestJSON    string `json:"request_json,omitempty"`
	InputsJSON     string `json:"inputs_json,omitempty"`
	ArtifactDigest string `json:"artifact_digest,omitempty"`
	ArtifactSize   int64  `json:"artifact_size,omitempty"`
	RunOutput      string `json:"run_output,omitempty"`
	Error          string `json:"error,omitempty"`
	CreatedAt      int64  `json:"created_at"`
	UpdatedAt      int64  `json:"updated_at"`
	LeaseExpiresAt int64  `json:"lease_expires_at,omitempty"`
}

type Store struct {
	db *sql.DB
}

// Open opens (creating if needed) the metadata database at path and
// recovers from any previous unclean shutdown: entries left in
// "building" state belong to a dead process and are marked failed.
func Open(path string) (*Store, error) {
	dsn := fmt.Sprintf("file:%s?_pragma=busy_timeout(10000)&_pragma=journal_mode(WAL)&_pragma=foreign_keys(1)", path)
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, err
	}
	// SQLite allows a single writer; one connection avoids SQLITE_BUSY
	// between our own goroutines.
	db.SetMaxOpenConns(1)
	if _, err := db.Exec(`
CREATE TABLE IF NOT EXISTS entries (
  key              TEXT PRIMARY KEY,
  status           TEXT NOT NULL,
  owner            TEXT NOT NULL DEFAULT '',
  request_json     TEXT NOT NULL DEFAULT '',
  inputs_json      TEXT NOT NULL DEFAULT '',
  artifact_digest  TEXT NOT NULL DEFAULT '',
  artifact_size    INTEGER NOT NULL DEFAULT 0,
  run_output       TEXT NOT NULL DEFAULT '',
  error            TEXT NOT NULL DEFAULT '',
  created_at       INTEGER NOT NULL,
  updated_at       INTEGER NOT NULL,
  lease_expires_at INTEGER NOT NULL DEFAULT 0
)`); err != nil {
		db.Close()
		return nil, err
	}
	res, err := db.Exec(`UPDATE entries SET status=?, error=?, updated_at=? WHERE status=?`,
		StatusFailed, "interrupted: server restarted while this build was running",
		time.Now().Unix(), StatusBuilding)
	if err != nil {
		db.Close()
		return nil, err
	}
	if n, _ := res.RowsAffected(); n > 0 {
		fmt.Printf("store: recovered %d interrupted build(s) from previous run\n", n)
	}
	return &Store{db: db}, nil
}

func (s *Store) Close() error { return s.db.Close() }

// TryAcquire attempts to become the publisher for key. It succeeds when
// no entry exists, when the existing entry is failed/quarantined, or when
// the current publisher's lease has expired (crashed publisher). It
// returns true iff this caller now owns the building lease.
func (s *Store) TryAcquire(key, owner, requestJSON, inputsJSON string, lease time.Duration) (bool, error) {
	now := time.Now().Unix()
	nowMs := time.Now().UnixMilli()
	expires := nowMs + lease.Milliseconds()
	_, err := s.db.Exec(`
INSERT INTO entries (key, status, owner, request_json, inputs_json, created_at, updated_at, lease_expires_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(key) DO UPDATE SET
  status           = excluded.status,
  owner            = excluded.owner,
  request_json     = excluded.request_json,
  inputs_json      = excluded.inputs_json,
  artifact_digest  = '',
  artifact_size    = 0,
  run_output       = '',
  error            = '',
  updated_at       = excluded.updated_at,
  lease_expires_at = excluded.lease_expires_at
WHERE entries.status IN (?, ?)
   OR (entries.status = ? AND entries.lease_expires_at < ?)`,
		key, StatusBuilding, owner, requestJSON, inputsJSON, now, now, expires,
		StatusFailed, StatusQuarantined, StatusBuilding, nowMs)
	if err != nil {
		return false, err
	}
	var ownerNow string
	err = s.db.QueryRow(`SELECT owner FROM entries WHERE key=? AND status=?`, key, StatusBuilding).Scan(&ownerNow)
	if err == sql.ErrNoRows {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	return ownerNow == owner, nil
}

// PublishSuccess records a successful build for a key owned by owner.
func (s *Store) PublishSuccess(key, owner, digest string, size int64, runOutput string) error {
	res, err := s.db.Exec(`
UPDATE entries SET status=?, artifact_digest=?, artifact_size=?, run_output=?, error='', updated_at=?, lease_expires_at=0
WHERE key=? AND owner=? AND status=?`,
		StatusSuccess, digest, size, runOutput, time.Now().Unix(), key, owner, StatusBuilding)
	if err != nil {
		return err
	}
	return expectOne(res, key)
}

// PublishFailure records a failed build. A failure is never a cache hit.
func (s *Store) PublishFailure(key, owner, buildErr string) error {
	res, err := s.db.Exec(`
UPDATE entries SET status=?, error=?, updated_at=?, lease_expires_at=0
WHERE key=? AND owner=? AND status=?`,
		StatusFailed, buildErr, time.Now().Unix(), key, owner, StatusBuilding)
	if err != nil {
		return err
	}
	return expectOne(res, key)
}

// MarkQuarantined flags an entry whose artifact failed verification.
func (s *Store) MarkQuarantined(key, reason string) error {
	_, err := s.db.Exec(`UPDATE entries SET status=?, error=?, updated_at=? WHERE key=?`,
		StatusQuarantined, reason, time.Now().Unix(), key)
	return err
}

func expectOne(res sql.Result, key string) error {
	n, err := res.RowsAffected()
	if err != nil {
		return err
	}
	if n != 1 {
		return fmt.Errorf("publish for key %s affected %d rows (lost lease?)", key[:12], n)
	}
	return nil
}

func (s *Store) Get(key string) (*Entry, error) {
	row := s.db.QueryRow(`SELECT key,status,owner,request_json,inputs_json,artifact_digest,artifact_size,run_output,error,created_at,updated_at,lease_expires_at FROM entries WHERE key=?`, key)
	return scan(row)
}

func (s *Store) List() ([]Entry, error) {
	rows, err := s.db.Query(`SELECT key,status,owner,request_json,inputs_json,artifact_digest,artifact_size,run_output,error,created_at,updated_at,lease_expires_at FROM entries ORDER BY updated_at DESC`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Entry
	for rows.Next() {
		e, err := scan(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, *e)
	}
	return out, rows.Err()
}

type scanner interface {
	Scan(dest ...any) error
}

func scan(row scanner) (*Entry, error) {
	var e Entry
	err := row.Scan(&e.Key, &e.Status, &e.Owner, &e.RequestJSON, &e.InputsJSON,
		&e.ArtifactDigest, &e.ArtifactSize, &e.RunOutput, &e.Error,
		&e.CreatedAt, &e.UpdatedAt, &e.LeaseExpiresAt)
	if err != nil {
		return nil, err
	}
	return &e, nil
}

// WaitFor polls until the entry leaves the building state or ctx ends.
func (s *Store) WaitFor(ctx context.Context, key string, poll time.Duration) (*Entry, error) {
	t := time.NewTicker(poll)
	defer t.Stop()
	for {
		e, err := s.Get(key)
		if err != nil && err != sql.ErrNoRows {
			return nil, err
		}
		if e == nil || e.Status != StatusBuilding {
			return e, nil
		}
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-t.C:
		}
	}
}
