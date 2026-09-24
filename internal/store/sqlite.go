// Package store persists workload configurations, config versions and
// autoscaling decisions in SQLite. It also implements the event-time
// regression guard: an evaluation whose metric timestamp is not newer than the
// stored decision for that workload is refused and never overwrites it.
package store

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sync"

	_ "github.com/mattn/go-sqlite3"

	"scaler/internal/scaler"
)

// ErrNotFound is returned when a workload or decision does not exist.
var ErrNotFound = errors.New("not found")

// ErrStaleMetric means the incoming metric timestamp is <= the newest stored
// decision timestamp for that workload (equal counts as a duplicate).
var ErrStaleMetric = errors.New("metric timestamp is not newer than the latest stored decision")

// Workload is the current configuration row of one workload.
type Workload struct {
	Name            string
	ConfigVersion   string
	MinReplicas     int
	MaxReplicas     int
	TargetPct       float64
	TolerancePct    float64
	StableWindowSec int
	UpdatedAtMs     int64
}

// Config is an alias so callers need only import store for persistence types.
type Config = scaler.Config

// DecisionRecord is one persisted evaluation with everything needed to audit
// it and to verify its signature.
type DecisionRecord struct {
	ID                int64
	Workload          string
	ConfigVersion     string
	MetricTimeMs      int64
	CreatedAtMs       int64
	CurrentReplicas   int
	ReadyCount        int
	UnreadyCount      int
	ReadyReporting    int
	ReadyMissing      int
	AvgUtilizationPct float64
	MetricPresent     bool
	TargetPct         float64
	Ratio             float64
	RawProposed       int
	Stabilized        int
	FinalReplicas     int
	Action            string
	Reasons           string // comma-separated reason codes
	Signature         string // hex HMAC-SHA256 of the canonical decision string
}

// Store wraps the SQLite database. A process-wide RWMutex serializes writers:
// decisions need a read-latest + insert + window-insert transaction that must
// not race another decision for the same workload.
type Store struct {
	db  *sql.DB
	mu  sync.RWMutex
	dsn string
}

const schema = `
CREATE TABLE IF NOT EXISTS workloads (
    name              TEXT PRIMARY KEY,
    config_version    TEXT NOT NULL,
    min_replicas      INTEGER NOT NULL,
    max_replicas      INTEGER NOT NULL,
    target_pct        REAL NOT NULL,
    tolerance_pct     REAL NOT NULL,
    stable_window_sec INTEGER NOT NULL,
    updated_at_ms     INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS config_versions (
    version           TEXT NOT NULL,
    workload          TEXT NOT NULL,
    min_replicas      INTEGER NOT NULL,
    max_replicas      INTEGER NOT NULL,
    target_pct        REAL NOT NULL,
    tolerance_pct     REAL NOT NULL,
    stable_window_sec INTEGER NOT NULL,
    created_at_ms     INTEGER NOT NULL,
    PRIMARY KEY (workload, version)
);
CREATE TABLE IF NOT EXISTS decisions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    workload            TEXT NOT NULL,
    config_version      TEXT NOT NULL,
    metric_time_ms      INTEGER NOT NULL,
    created_at_ms       INTEGER NOT NULL,
    current_replicas    INTEGER NOT NULL,
    ready_count         INTEGER NOT NULL,
    unready_count       INTEGER NOT NULL,
    ready_reporting     INTEGER NOT NULL,
    ready_missing       INTEGER NOT NULL,
    avg_utilization_pct REAL NOT NULL,
    metric_present      INTEGER NOT NULL,
    target_pct          REAL NOT NULL,
    ratio               REAL NOT NULL,
    raw_proposed        INTEGER NOT NULL,
    stabilized          INTEGER NOT NULL,
    final_replicas      INTEGER NOT NULL,
    action              TEXT NOT NULL,
    reasons             TEXT NOT NULL,
    signature           TEXT NOT NULL,
    UNIQUE (workload, metric_time_ms)
);
CREATE INDEX IF NOT EXISTS idx_decisions_workload_metric
    ON decisions (workload, metric_time_ms DESC);
CREATE TABLE IF NOT EXISTS window_proposals (
    workload       TEXT NOT NULL,
    config_version TEXT NOT NULL,
    metric_time_ms INTEGER NOT NULL,
    raw_proposed   INTEGER NOT NULL,
    PRIMARY KEY (workload, config_version, metric_time_ms)
);
`

// Open opens (creating if needed) the SQLite database at path and applies the
// schema. Use ":memory:" for ephemeral test databases.
func Open(ctx context.Context, path string) (*Store, error) {
	dsn := path
	if path != ":memory:" {
		dsn = path + "?_busy_timeout=5000&_journal_mode=WAL&_foreign_keys=on"
		if dir := filepath.Dir(path); dir != "" && dir != "." {
			if err := os.MkdirAll(dir, 0o755); err != nil {
				return nil, fmt.Errorf("create db directory: %w", err)
			}
		}
	}
	db, err := sql.Open("sqlite3", dsn)
	if err != nil {
		return nil, fmt.Errorf("open sqlite: %w", err)
	}
	db.SetMaxOpenConns(1) // avoid SQLITE_BUSY across writers; deterministic tests
	if err := db.PingContext(ctx); err != nil {
		db.Close()
		return nil, fmt.Errorf("ping sqlite: %w", err)
	}
	if _, err := db.ExecContext(ctx, schema); err != nil {
		db.Close()
		return nil, fmt.Errorf("apply schema: %w", err)
	}
	return &Store{db: db, dsn: dsn}, nil
}

// Close releases the database handle.
func (s *Store) Close() error { return s.db.Close() }

// PingDB checks database connectivity.
func (s *Store) PingDB(ctx context.Context) error { return s.db.PingContext(ctx) }

// UpsertWorkload stores the configuration, records the version row and returns
// the resulting workload state.
func (s *Store) UpsertWorkload(ctx context.Context, name, version string, cfg Config, nowMs int64) (Workload, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return Workload{}, err
	}
	defer tx.Rollback()

	if _, err := tx.ExecContext(ctx, `
        INSERT INTO workloads (name, config_version, min_replicas, max_replicas,
            target_pct, tolerance_pct, stable_window_sec, updated_at_ms)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            config_version=excluded.config_version,
            min_replicas=excluded.min_replicas,
            max_replicas=excluded.max_replicas,
            target_pct=excluded.target_pct,
            tolerance_pct=excluded.tolerance_pct,
            stable_window_sec=excluded.stable_window_sec,
            updated_at_ms=excluded.updated_at_ms`,
		name, version, cfg.MinReplicas, cfg.MaxReplicas, cfg.TargetPct,
		cfg.TolerancePct, cfg.StableWindowSec, nowMs); err != nil {
		return Workload{}, err
	}
	if _, err := tx.ExecContext(ctx, `
        INSERT OR IGNORE INTO config_versions
            (version, workload, min_replicas, max_replicas, target_pct,
             tolerance_pct, stable_window_sec, created_at_ms)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
		version, name, cfg.MinReplicas, cfg.MaxReplicas, cfg.TargetPct,
		cfg.TolerancePct, cfg.StableWindowSec, nowMs); err != nil {
		return Workload{}, err
	}
	if err := tx.Commit(); err != nil {
		return Workload{}, err
	}
	return s.getWorkloadLocked(ctx, name)
}

// GetWorkload returns the current configuration of a workload.
func (s *Store) GetWorkload(ctx context.Context, name string) (Workload, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.getWorkloadLocked(ctx, name)
}

// getWorkloadLocked performs the query without taking the mutex; the caller
// MUST already hold s.mu (RWMutex is not reentrant).
func (s *Store) getWorkloadLocked(ctx context.Context, name string) (Workload, error) {
	row := s.db.QueryRowContext(ctx, `
        SELECT name, config_version, min_replicas, max_replicas, target_pct,
               tolerance_pct, stable_window_sec, updated_at_ms
        FROM workloads WHERE name = ?`, name)
	var w Workload
	err := row.Scan(&w.Name, &w.ConfigVersion, &w.MinReplicas, &w.MaxReplicas,
		&w.TargetPct, &w.TolerancePct, &w.StableWindowSec, &w.UpdatedAtMs)
	if errors.Is(err, sql.ErrNoRows) {
		return Workload{}, ErrNotFound
	}
	return w, err
}

// WindowEntries returns raw proposals strictly older than beforeMs within the
// look-back window [beforeMs-windowMs, beforeMs), for the given config version.
func (s *Store) WindowEntries(ctx context.Context, workload, configVersion string, beforeMs, windowMs int64) ([]scaler.WindowEntry, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	cutoff := beforeMs - windowMs
	rows, err := s.db.QueryContext(ctx, `
        SELECT metric_time_ms, raw_proposed
        FROM window_proposals
        WHERE workload = ? AND config_version = ?
          AND metric_time_ms < ? AND metric_time_ms >= ?
        ORDER BY metric_time_ms ASC`,
		workload, configVersion, beforeMs, cutoff)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []scaler.WindowEntry
	for rows.Next() {
		var e scaler.WindowEntry
		if err := rows.Scan(&e.MetricTimeMs, &e.RawProposed); err != nil {
			return nil, err
		}
		out = append(out, e)
	}
	return out, rows.Err()
}

// LatestMetricTime returns the greatest stored metric timestamp for a
// workload. A workload with no decisions returns (0, false, nil).
func (s *Store) LatestMetricTime(ctx context.Context, workload string) (int64, bool, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	var ts int64
	err := s.db.QueryRowContext(ctx,
		`SELECT COALESCE(MAX(metric_time_ms), 0) FROM decisions WHERE workload = ?`,
		workload).Scan(&ts)
	if err != nil {
		return 0, false, err
	}
	return ts, ts > 0, nil
}

// LatestDecision returns the newest decision (by metric time) for a workload.
func (s *Store) LatestDecision(ctx context.Context, workload string) (DecisionRecord, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.queryDecision(ctx, `
        SELECT id, workload, config_version, metric_time_ms, created_at_ms,
               current_replicas, ready_count, unready_count, ready_reporting,
               ready_missing, avg_utilization_pct, metric_present, target_pct,
               ratio, raw_proposed, stabilized, final_replicas, action,
               reasons, signature
        FROM decisions WHERE workload = ? ORDER BY metric_time_ms DESC LIMIT 1`,
		workload)
}

// ListDecisions returns decisions for a workload ordered newest-first, with a
// limit (clamped to [1,500]).
func (s *Store) ListDecisions(ctx context.Context, workload string, limit int) ([]DecisionRecord, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if limit <= 0 {
		limit = 50
	}
	if limit > 500 {
		limit = 500
	}
	rows, err := s.db.QueryContext(ctx, `
        SELECT id, workload, config_version, metric_time_ms, created_at_ms,
               current_replicas, ready_count, unready_count, ready_reporting,
               ready_missing, avg_utilization_pct, metric_present, target_pct,
               ratio, raw_proposed, stabilized, final_replicas, action,
               reasons, signature
        FROM decisions WHERE workload = ? ORDER BY metric_time_ms DESC LIMIT ?`,
		workload, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []DecisionRecord
	for rows.Next() {
		d, err := scanDecision(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, d)
	}
	return out, rows.Err()
}

// InsertDecision persists a decision and, when rawProposed >= 0, records the
// raw proposal in the stable-window table. It enforces the monotonic event-time
// rule: metricTimeMs must be strictly greater than the latest stored one.
func (s *Store) InsertDecision(ctx context.Context, r DecisionRecord, rawProposed int) (int64, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return 0, err
	}
	defer tx.Rollback()

	var latest sql.NullInt64
	if err := tx.QueryRowContext(ctx,
		`SELECT MAX(metric_time_ms) FROM decisions WHERE workload = ?`,
		r.Workload).Scan(&latest); err != nil {
		return 0, err
	}
	if latest.Valid && r.MetricTimeMs <= latest.Int64 {
		return 0, ErrStaleMetric
	}

	res, err := tx.ExecContext(ctx, `
        INSERT INTO decisions (workload, config_version, metric_time_ms,
            created_at_ms, current_replicas, ready_count, unready_count,
            ready_reporting, ready_missing, avg_utilization_pct, metric_present,
            target_pct, ratio, raw_proposed, stabilized, final_replicas,
            action, reasons, signature)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
		r.Workload, r.ConfigVersion, r.MetricTimeMs, r.CreatedAtMs,
		r.CurrentReplicas, r.ReadyCount, r.UnreadyCount, r.ReadyReporting,
		r.ReadyMissing, r.AvgUtilizationPct, r.MetricPresent, r.TargetPct,
		r.Ratio, r.RawProposed, r.Stabilized, r.FinalReplicas, r.Action,
		r.Reasons, r.Signature)
	if err != nil {
		return 0, err
	}
	id, err := res.LastInsertId()
	if err != nil {
		return 0, err
	}
	if rawProposed >= 0 {
		if _, err := tx.ExecContext(ctx, `
            INSERT OR REPLACE INTO window_proposals
                (workload, config_version, metric_time_ms, raw_proposed)
            VALUES (?,?,?,?)`,
			r.Workload, r.ConfigVersion, r.MetricTimeMs, rawProposed); err != nil {
			return 0, err
		}
	}
	if err := tx.Commit(); err != nil {
		return 0, err
	}
	return id, nil
}

func (s *Store) queryDecision(ctx context.Context, query string, args ...any) (DecisionRecord, error) {
	row := s.db.QueryRowContext(ctx, query, args...)
	d, err := scanDecision(row)
	if errors.Is(err, sql.ErrNoRows) {
		return DecisionRecord{}, ErrNotFound
	}
	return d, err
}

type rowScanner interface {
	Scan(dest ...any) error
}

func scanDecision(sc rowScanner) (DecisionRecord, error) {
	var d DecisionRecord
	var metricPresent int
	err := sc.Scan(&d.ID, &d.Workload, &d.ConfigVersion, &d.MetricTimeMs,
		&d.CreatedAtMs, &d.CurrentReplicas, &d.ReadyCount, &d.UnreadyCount,
		&d.ReadyReporting, &d.ReadyMissing, &d.AvgUtilizationPct, &metricPresent,
		&d.TargetPct, &d.Ratio, &d.RawProposed, &d.Stabilized, &d.FinalReplicas,
		&d.Action, &d.Reasons, &d.Signature)
	d.MetricPresent = metricPresent != 0
	return d, err
}
