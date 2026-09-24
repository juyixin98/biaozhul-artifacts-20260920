// Package store provides the SQLite persistence layer for the scaling
// decision engine.
package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"sync"
	"time"

	"scaler-stable-window/internal/hpa"

	_ "modernc.org/sqlite"
)

// Store is the SQLite-backed implementation of hpa.Store.
type Store struct {
	db *sql.DB
	mu sync.Mutex // serializes write transactions (SQLite single-writer)
}

// New opens (creating the schema if needed) the database at dsn.
func New(dsn string) (*Store, error) {
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, err
	}
	// One connection serializes access; this is a local decision engine where
	// event-time correctness matters more than write throughput.
	db.SetMaxOpenConns(1)
	db.SetMaxIdleConns(1)
	db.SetConnMaxLifetime(0)
	if _, err := db.ExecContext(context.Background(), pragmas); err != nil {
		return nil, fmt.Errorf("set pragmas: %w", err)
	}
	s := &Store{db: db}
	if err := s.migrate(); err != nil {
		return nil, fmt.Errorf("migrate: %w", err)
	}
	return s, nil
}

func (s *Store) Close() error { return s.db.Close() }

const pragmas = `
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
PRAGMA foreign_keys=ON;
PRAGMA synchronous=NORMAL;
`

const schema = `
CREATE TABLE IF NOT EXISTS scalers (
    id         TEXT PRIMARY KEY,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS configs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scaler_id   TEXT NOT NULL,
    version     INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    body        TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    UNIQUE(scaler_id, version),
    FOREIGN KEY(scaler_id) REFERENCES scalers(id)
);
CREATE TABLE IF NOT EXISTS recommendations (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    scaler_id      TEXT NOT NULL,
    config_version INTEGER NOT NULL,
    metric_ts_ns   INTEGER NOT NULL,
    raw_desired    INTEGER NOT NULL,
    created_at_ns  INTEGER NOT NULL,
    UNIQUE(scaler_id, metric_ts_ns),
    FOREIGN KEY(scaler_id) REFERENCES scalers(id)
);
CREATE INDEX IF NOT EXISTS idx_rec_window
    ON recommendations(scaler_id, config_version, metric_ts_ns);
CREATE TABLE IF NOT EXISTS decisions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    scaler_id      TEXT NOT NULL,
    config_version INTEGER NOT NULL,
    metric_ts_ns   INTEGER NOT NULL,
    decided_at_ns  INTEGER NOT NULL,
    final_desired  INTEGER NOT NULL,
    final_action   TEXT NOT NULL,
    body           TEXT NOT NULL,
    UNIQUE(scaler_id, metric_ts_ns),
    FOREIGN KEY(scaler_id) REFERENCES scalers(id)
);
CREATE TABLE IF NOT EXISTS scaler_meta (
    scaler_id      TEXT PRIMARY KEY,
    last_metric_ns INTEGER NOT NULL,
    FOREIGN KEY(scaler_id) REFERENCES scalers(id)
);
`

func (s *Store) migrate() error {
	_, err := s.db.ExecContext(context.Background(), schema)
	return err
}

// SaveConfig upserts the scaler row and stores a new immutable config
// version (scaler_id+version is unique).
func (s *Store) SaveConfig(scalerID, fingerprint string, version int64, cfgJSON map[string]any, createdAt time.Time) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	body, err := json.Marshal(cfgJSON)
	if err != nil {
		return err
	}
	tx, err := s.db.BeginTx(context.Background(), nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if _, err := tx.Exec(
		`INSERT OR IGNORE INTO scalers(id, created_at) VALUES(?,?)`,
		scalerID, createdAt.UnixNano()); err != nil {
		return err
	}
	if _, err := tx.Exec(
		`INSERT INTO configs(scaler_id, version, fingerprint, body, created_at)
		 VALUES(?,?,?,?,?)`,
		scalerID, version, fingerprint, string(body), createdAt.UnixNano()); err != nil {
		return err
	}
	return tx.Commit()
}

// ConfigSnapshot is one stored config version.
type ConfigSnapshot struct {
	Version     int64          `json:"version"`
	Fingerprint string         `json:"fingerprint"`
	CreatedAt   time.Time      `json:"createdAt"`
	Body        map[string]any `json:"body"`
}

// LatestConfig returns the highest-version config for a scaler.
func (s *Store) LatestConfig(scalerID string) (*ConfigSnapshot, bool, error) {
	var body, fp string
	var version, createdAt int64
	err := s.db.QueryRowContext(context.Background(),
		`SELECT body, version, fingerprint, created_at FROM configs
		 WHERE scaler_id=? ORDER BY version DESC LIMIT 1`, scalerID).
		Scan(&body, &version, &fp, &createdAt)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, err
	}
	var m map[string]any
	if err := json.Unmarshal([]byte(body), &m); err != nil {
		return nil, false, err
	}
	return &ConfigSnapshot{
		Version:     version,
		Fingerprint: fp,
		CreatedAt:   time.Unix(0, createdAt).UTC(),
		Body:        m,
	}, true, nil
}

// ListConfigs returns all config versions (newest first).
func (s *Store) ListConfigs(scalerID string) ([]ConfigSnapshot, error) {
	rows, err := s.db.QueryContext(context.Background(),
		`SELECT body, version, fingerprint, created_at FROM configs
		 WHERE scaler_id=? ORDER BY version DESC`, scalerID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []ConfigSnapshot
	for rows.Next() {
		var snap ConfigSnapshot
		var body string
		var created int64
		if err := rows.Scan(&body, &snap.Version, &snap.Fingerprint, &created); err != nil {
			return nil, err
		}
		snap.CreatedAt = time.Unix(0, created).UTC()
		if err := json.Unmarshal([]byte(body), &snap.Body); err != nil {
			return nil, err
		}
		out = append(out, snap)
	}
	return out, rows.Err()
}

// DecisionRow is the audit projection of one decision.
type DecisionRow struct {
	ScalerID      string          `json:"scalerId"`
	ConfigVersion int64           `json:"configVersion"`
	MetricTSNS    int64           `json:"metricTsNs"`
	DecidedAtNS   int64           `json:"decidedAtNs"`
	FinalDesired  int             `json:"finalDesired"`
	FinalAction   string          `json:"finalAction"`
	Body          json.RawMessage `json:"body"`
}

// ListDecisions returns stored decisions (newest event time first).
func (s *Store) ListDecisions(ctx context.Context, scalerID string, limit int) ([]DecisionRow, error) {
	if limit <= 0 || limit > 1000 {
		limit = 100
	}
	rows, err := s.db.QueryContext(ctx,
		`SELECT scaler_id, config_version, metric_ts_ns, decided_at_ns,
		        final_desired, final_action, body
		 FROM decisions WHERE scaler_id=? ORDER BY metric_ts_ns DESC LIMIT ?`,
		scalerID, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []DecisionRow
	for rows.Next() {
		var r DecisionRow
		var body string
		if err := rows.Scan(&r.ScalerID, &r.ConfigVersion, &r.MetricTSNS,
			&r.DecidedAtNS, &r.FinalDesired, &r.FinalAction, &body); err != nil {
			return nil, err
		}
		r.Body = json.RawMessage(body)
		out = append(out, r)
	}
	return out, rows.Err()
}

// RecommendationRow is one stored raw recommendation.
type RecommendationRow struct {
	ConfigVersion int64 `json:"configVersion"`
	MetricTSNS    int64 `json:"metricTsNs"`
	RawDesired    int   `json:"rawDesired"`
}

// ListRecommendations returns the stable-window inputs (newest first).
func (s *Store) ListRecommendations(ctx context.Context, scalerID string, limit int) ([]RecommendationRow, error) {
	if limit <= 0 || limit > 1000 {
		limit = 100
	}
	rows, err := s.db.QueryContext(ctx,
		`SELECT config_version, metric_ts_ns, raw_desired FROM recommendations
		 WHERE scaler_id=? ORDER BY metric_ts_ns DESC LIMIT ?`, scalerID, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []RecommendationRow
	for rows.Next() {
		var r RecommendationRow
		if err := rows.Scan(&r.ConfigVersion, &r.MetricTSNS, &r.RawDesired); err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

// GetDecision fetches one decision by scaler and event time.
func (s *Store) GetDecision(ctx context.Context, scalerID string, ts time.Time) (json.RawMessage, bool, error) {
	var body string
	err := s.db.QueryRowContext(ctx,
		`SELECT body FROM decisions WHERE scaler_id=? AND metric_ts_ns=?`,
		scalerID, ts.UnixNano()).Scan(&body)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, err
	}
	return json.RawMessage(body), true, nil
}

// RunDecideTx implements hpa.Store: the engine callback runs inside a
// serialized SQLite transaction.
func (s *Store) RunDecideTx(ctx context.Context, scalerID string, fn func(tx hpa.Tx) error) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	sqlTx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer sqlTx.Rollback()

	if _, err := sqlTx.ExecContext(ctx,
		`INSERT OR IGNORE INTO scalers(id, created_at) VALUES(?,?)`,
		scalerID, time.Now().UnixNano()); err != nil {
		return err
	}

	if err := fn(&tx{ctx: ctx, tx: sqlTx, scalerID: scalerID}); err != nil {
		return err
	}
	return sqlTx.Commit()
}
