// Package store is the SQLite persistence layer for the build cache.
package store

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"time"

	_ "modernc.org/sqlite"
)

// Entry statuses.
const (
	StatusBuilding    = "building"    // publisher lease held, no artifact yet
	StatusSucceeded   = "succeeded"   // artifact published and verified
	StatusFailed      = "failed"      // build failed; recorded but never a hit
	StatusQuarantined = "quarantined" // artifact corrupt; isolated
	StatusInterrupted = "interrupted" // was building when the server died
)

// ErrNotFound is returned when no row exists for a key.
var ErrNotFound = errors.New("cache entry not found")

// Entry is one cache row.
type Entry struct {
	Key          string
	Status       string
	Attempt      int
	ArtifactPath string // relative to data dir; "" unless succeeded
	ArtifactSize int64
	ArtifactSHA  string
	ExitCode     int
	Stdout       string
	Stderr       string
	ErrMessage   string
	CreatedAt    time.Time
	UpdatedAt    time.Time
	LeaseSince   time.Time
}

// QuarantineEvent records why an entry was isolated.
type QuarantineEvent struct {
	ID          int64
	Key         string
	Reason      string
	Detail      string
	OldSHA      string
	ObservedSHA string
	CreatedAt   time.Time
}

// Store wraps the database and the artifact blob directory.
type Store struct {
	db      *sql.DB
	dataDir string
}

// Open opens (creating if needed) the database at dbPath and runs migrations.
func Open(ctx context.Context, dbPath, dataDir string) (*Store, error) {
	// WAL for a single busy writer + concurrent readers; busy_timeout lets
	// writers wait instead of failing under contention; foreign keys on.
	dsn := fmt.Sprintf("file:%s?_pragma=busy_timeout(5000)&_pragma=journal_mode(WAL)&_pragma=foreign_keys(ON)&_pragma=synchronous(NORMAL)", dbPath)
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, err
	}
	// modernc is safest with one writer connection; reads can share.
	db.SetMaxOpenConns(1)
	if err := db.PingContext(ctx); err != nil {
		db.Close()
		return nil, err
	}
	s := &Store{db: db, dataDir: dataDir}
	if err := s.migrate(ctx); err != nil {
		db.Close()
		return nil, err
	}
	return s, nil
}

// Close closes the database.
func (s *Store) Close() error { return s.db.Close() }

func (s *Store) migrate(ctx context.Context) error {
	_, err := s.db.ExecContext(ctx, `
CREATE TABLE IF NOT EXISTS entries (
    key           TEXT PRIMARY KEY,
    status        TEXT NOT NULL,
    attempt       INTEGER NOT NULL DEFAULT 0,
    artifact_path TEXT NOT NULL DEFAULT '',
    artifact_size INTEGER NOT NULL DEFAULT 0,
    artifact_sha  TEXT NOT NULL DEFAULT '',
    exit_code     INTEGER NOT NULL DEFAULT -1,
    stdout        TEXT NOT NULL DEFAULT '',
    stderr        TEXT NOT NULL DEFAULT '',
    err_message   TEXT NOT NULL DEFAULT '',
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL,
    lease_since   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS quarantine_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    key          TEXT NOT NULL,
    reason       TEXT NOT NULL,
    detail       TEXT NOT NULL DEFAULT '',
    old_sha      TEXT NOT NULL DEFAULT '',
    observed_sha TEXT NOT NULL DEFAULT '',
    created_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_qevents_key ON quarantine_events(key);
`)
	return err
}

// ResetStaleLeases marks entries left in "building" by a previous process as
// interrupted, so they become claimable again. A surviving process never
// matches because lease_since equals its start (see Recover called at boot).
func (s *Store) ResetStaleLeases(ctx context.Context) (int64, error) {
	now := time.Now().UnixNano()
	res, err := s.db.ExecContext(ctx,
		`UPDATE entries SET status=?, updated_at=? WHERE status=?`,
		StatusInterrupted, now, StatusBuilding)
	if err != nil {
		return 0, err
	}
	n, _ := res.RowsAffected()
	return n, nil
}

// Claim implements single-publisher-per-key.
//
// It returns claimed=true when the caller now owns the lease (the row is in
// status "building" with attempt incremented). It returns claimed=false with
// a non-nil entry when a succeeded/failed entry exists (caller may use it) or
// another publisher holds the lease (status "building").
func (s *Store) Claim(ctx context.Context, key string) (bool, *Entry, error) {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return false, nil, err
	}
	defer tx.Rollback()

	now := time.Now().UnixNano()
	var e Entry
	var createdAt, updatedAt, leaseSince int64
	var artifactPath, artifactSHA, stdout, stderr, errMsg string
	var artifactSize int64
	var exitCode int
	err = tx.QueryRowContext(ctx,
		`SELECT key,status,attempt,artifact_path,artifact_size,artifact_sha,
		        exit_code,stdout,stderr,err_message,created_at,updated_at,lease_since
		 FROM entries WHERE key=?`, key).Scan(
		&e.Key, &e.Status, &e.Attempt, &artifactPath, &artifactSize, &artifactSHA,
		&exitCode, &stdout, &stderr, &errMsg, &createdAt, &updatedAt, &leaseSince)
	switch {
	case errors.Is(err, sql.ErrNoRows):
		if _, err := tx.ExecContext(ctx,
			`INSERT INTO entries(key,status,attempt,created_at,updated_at,lease_since)
			 VALUES(?,?,?,?,?,?)`,
			key, StatusBuilding, 1, now, now, now); err != nil {
			return false, nil, err
		}
		if err := tx.Commit(); err != nil {
			return false, nil, err
		}
		return true, &Entry{Key: key, Status: StatusBuilding, Attempt: 1,
			CreatedAt: time.Unix(0, now), UpdatedAt: time.Unix(0, now), LeaseSince: time.Unix(0, now)}, nil
	case err != nil:
		return false, nil, err
	}

	e.ArtifactPath = artifactPath
	e.ArtifactSize = artifactSize
	e.ArtifactSHA = artifactSHA
	e.ExitCode = exitCode
	e.Stdout = stdout
	e.Stderr = stderr
	e.ErrMessage = errMsg
	e.CreatedAt = time.Unix(0, createdAt)
	e.UpdatedAt = time.Unix(0, updatedAt)
	e.LeaseSince = time.Unix(0, leaseSince)

	if e.Status == StatusBuilding {
		// Another live publisher owns it.
		return false, &e, nil
	}
	// succeeded / failed / quarantined / interrupted -> claim for rebuild.
	newAttempt := e.Attempt + 1
	if _, err := tx.ExecContext(ctx,
		`UPDATE entries SET status=?, attempt=?, artifact_path='', artifact_size=0,
		 artifact_sha='', exit_code=-1, stdout='', stderr='', err_message='',
		 updated_at=?, lease_since=? WHERE key=?`,
		StatusBuilding, newAttempt, now, now, key); err != nil {
		return false, nil, err
	}
	if err := tx.Commit(); err != nil {
		return false, nil, err
	}
	e.Status = StatusBuilding
	e.Attempt = newAttempt
	e.ArtifactPath = ""
	e.ArtifactSize = 0
	e.ArtifactSHA = ""
	e.LeaseSince = time.Unix(0, now)
	return true, &e, nil
}

// PublishResult records a completed build.
//
// On success an artifact path/size/sha must be supplied and the row becomes
// "succeeded". On failure the row becomes "failed" with NO artifact fields:
// a failed build can never be cached as, or later read as, a success.
func (s *Store) PublishResult(ctx context.Context, e *Entry) error {
	if e.Status != StatusSucceeded && e.Status != StatusFailed {
		return fmt.Errorf("PublishResult status must be succeeded or failed, got %q", e.Status)
	}
	if e.Status == StatusSucceeded && (e.ArtifactPath == "" || e.ArtifactSHA == "") {
		return errors.New("success publish requires artifact path and sha")
	}
	if e.Status == StatusFailed {
		// Defensive: never persist artifact fields on a failure.
		e.ArtifactPath, e.ArtifactSHA = "", ""
		e.ArtifactSize = 0
	}
	now := time.Now().UnixNano()
	res, err := s.db.ExecContext(ctx,
		`UPDATE entries SET status=?, artifact_path=?, artifact_size=?, artifact_sha=?,
		 exit_code=?, stdout=?, stderr=?, err_message=?, updated_at=?, lease_since=0
		 WHERE key=? AND status=?`,
		e.Status, e.ArtifactPath, e.ArtifactSize, e.ArtifactSHA,
		e.ExitCode, truncate(e.Stdout), truncate(e.Stderr), truncate(e.ErrMessage),
		now, e.Key, StatusBuilding)
	if err != nil {
		return err
	}
	if n, _ := res.RowsAffected(); n != 1 {
		return fmt.Errorf("publish lost the lease for key %s (rows=%d)", e.Key, n)
	}
	e.UpdatedAt = time.Unix(0, now)
	return nil
}

func truncate(s string) string {
	const max = 64 << 10 // 64 KiB per stream
	if len(s) > max {
		return s[:max] + "\n...[truncated]"
	}
	return s
}

// Get reads one entry. Missing rows return ErrNotFound.
func (s *Store) Get(ctx context.Context, key string) (*Entry, error) {
	var e Entry
	var createdAt, updatedAt, leaseSince int64
	var artifactPath, artifactSHA, stdout, stderr, errMsg string
	var artifactSize int64
	var exitCode int
	err := s.db.QueryRowContext(ctx,
		`SELECT key,status,attempt,artifact_path,artifact_size,artifact_sha,
		        exit_code,stdout,stderr,err_message,created_at,updated_at,lease_since
		 FROM entries WHERE key=?`, key).Scan(
		&e.Key, &e.Status, &e.Attempt, &artifactPath, &artifactSize, &artifactSHA,
		&exitCode, &stdout, &stderr, &errMsg, &createdAt, &updatedAt, &leaseSince)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, err
	}
	e.ArtifactPath, e.ArtifactSize, e.ArtifactSHA = artifactPath, artifactSize, artifactSHA
	e.ExitCode = exitCode
	e.Stdout, e.Stderr, e.ErrMessage = stdout, stderr, errMsg
	e.CreatedAt, e.UpdatedAt, e.LeaseSince = time.Unix(0, createdAt), time.Unix(0, updatedAt), time.Unix(0, leaseSince)
	return &e, nil
}

// Quarantine moves a succeeded entry to quarantine and logs the event. The
// artifact blob itself is moved by the caller (builder), which knows the
// filesystem layout; this call only updates durable state.
func (s *Store) Quarantine(ctx context.Context, key, reason, detail, oldSHA, observedSHA string) (*QuarantineEvent, error) {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	now := time.Now().UnixNano()
	res, err := tx.ExecContext(ctx,
		`INSERT INTO quarantine_events(key,reason,detail,old_sha,observed_sha,created_at)
		 VALUES(?,?,?,?,?,?)`, key, reason, detail, oldSHA, observedSHA, now)
	if err != nil {
		return nil, err
	}
	id, _ := res.LastInsertId()
	if _, err := tx.ExecContext(ctx,
		`UPDATE entries SET status=?, artifact_path='', artifact_size=0, artifact_sha='',
		 err_message=?, updated_at=? WHERE key=?`,
		StatusQuarantined, reason, now, key); err != nil {
		return nil, err
	}
	if err := tx.Commit(); err != nil {
		return nil, err
	}
	return &QuarantineEvent{ID: id, Key: key, Reason: reason, Detail: detail,
		OldSHA: oldSHA, ObservedSHA: observedSHA, CreatedAt: time.Unix(0, now)}, nil
}

// QuarantineEvents returns quarantine events for a key, newest last.
func (s *Store) QuarantineEvents(ctx context.Context, key string) ([]QuarantineEvent, error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT id,key,reason,detail,old_sha,observed_sha,created_at
		 FROM quarantine_events WHERE key=? ORDER BY id`, key)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []QuarantineEvent
	for rows.Next() {
		var q QuarantineEvent
		var ts int64
		if err := rows.Scan(&q.ID, &q.Key, &q.Reason, &q.Detail, &q.OldSHA, &q.ObservedSHA, &ts); err != nil {
			return nil, err
		}
		q.CreatedAt = time.Unix(0, ts)
		out = append(out, q)
	}
	return out, rows.Err()
}

// CountEntries returns total and per-status counts (for tests/diagnostics).
func (s *Store) CountEntries(ctx context.Context) (map[string]int, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT status, COUNT(*) FROM entries GROUP BY status`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[string]int{}
	for rows.Next() {
		var st string
		var n int
		if err := rows.Scan(&st, &n); err != nil {
			return nil, err
		}
		out[st] = n
	}
	return out, rows.Err()
}
