// Package storage owns the PostgreSQL schema and all SQL for raw samples
// and immutable window versions.
package storage

import (
	"context"
	"errors"
	"fmt"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"twap/internal/twap"
)

// DB wraps the connection pool.
type DB struct {
	Pool *pgxpool.Pool
}

// New connects and verifies the database.
func New(ctx context.Context, url string) (*DB, error) {
	cfg, err := pgxpool.ParseConfig(url)
	if err != nil {
		return nil, fmt.Errorf("parse database url: %w", err)
	}
	cfg.MaxConns = 10
	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		return nil, fmt.Errorf("connect database: %w", err)
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, fmt.Errorf("ping database: %w", err)
	}
	return &DB{Pool: pool}, nil
}

// Close releases the pool.
func (d *DB) Close() { d.Pool.Close() }

// EnsureSchema creates tables and indexes idempotently.
func (d *DB) EnsureSchema(ctx context.Context) error {
	const ddl = `
CREATE TABLE IF NOT EXISTS samples (
    ts          BIGINT      NOT NULL,
    source      TEXT        NOT NULL,
    price       BIGINT      NOT NULL,
    received_at BIGINT      NOT NULL,
    PRIMARY KEY (ts, source)
);
COMMENT ON TABLE samples IS 'Raw price observations; (ts, source) is the natural key.';

CREATE TABLE IF NOT EXISTS window_versions (
    window_start    BIGINT      NOT NULL,
    version         INTEGER     NOT NULL,
    window_end      BIGINT      NOT NULL,
    input_hash      TEXT        NOT NULL,
    signature       TEXT        NOT NULL,
    twap_num        TEXT,
    twap_den        TEXT,
    covered_micros  BIGINT      NOT NULL,
    window_micros   BIGINT      NOT NULL,
    conflict_count  INTEGER     NOT NULL,
    stale           BOOLEAN     NOT NULL,
    last_sample_ts  BIGINT      NOT NULL,
    has_anchor      BOOLEAN     NOT NULL,
    samples_used    INTEGER     NOT NULL,
    segments        JSONB       NOT NULL,
    computed_at     BIGINT      NOT NULL,
    PRIMARY KEY (window_start, version)
);
COMMENT ON TABLE window_versions IS 'Append-only materialized TWAP versions per window.';
CREATE INDEX IF NOT EXISTS window_versions_end_idx
    ON window_versions (window_end);
`
	_, err := d.Pool.Exec(ctx, ddl)
	return err
}

// ErrNotFound is returned when no row exists.
var ErrNotFound = errors.New("not found")

// UpsertSample inserts a raw sample or updates an existing (ts, source)
// row. changed reports whether the stored price changed (a pure
// re-insertion of the same value is idempotent).
func (d *DB) UpsertSample(ctx context.Context, s twap.Sample, receivedAt int64) (changed bool, err error) {
	tx, err := d.Pool.Begin(ctx)
	if err != nil {
		return false, err
	}
	defer tx.Rollback(ctx)

	var oldPrice *int64
	err = tx.QueryRow(ctx,
		`SELECT price FROM samples WHERE ts = $1 AND source = $2`,
		s.TS, s.Source).Scan(&oldPrice)
	switch {
	case errors.Is(err, pgx.ErrNoRows):
		_, err = tx.Exec(ctx,
			`INSERT INTO samples (ts, source, price, received_at)
			 VALUES ($1, $2, $3, $4)`,
			s.TS, s.Source, s.Price, receivedAt)
		changed = true
	case err != nil:
		return false, err
	default:
		if *oldPrice != s.Price {
			_, err = tx.Exec(ctx,
				`UPDATE samples SET price = $3, received_at = $4
				 WHERE ts = $1 AND source = $2`,
				s.TS, s.Source, s.Price, receivedAt)
			changed = true
		}
	}
	if err != nil {
		return false, err
	}
	if err := tx.Commit(ctx); err != nil {
		return false, err
	}
	return changed, nil
}

// LoadWindowSamples returns every sample that can affect the window
// [wStart, wEnd) as of horizon (samples with ts >= horizon are not yet
// visible). This is:
//   - for each source, its newest sample with ts <= wStart (the anchor),
//   - every sample strictly inside (wStart, horizon).
func (d *DB) LoadWindowSamples(ctx context.Context, wStart, wEnd, horizon int64) ([]twap.Sample, error) {
	rows, err := d.Pool.Query(ctx, `
SELECT ts, source, price FROM (
    SELECT DISTINCT ON (source) ts, source, price
    FROM samples
    WHERE ts <= $1
    ORDER BY source, ts DESC
) anchor
UNION ALL
SELECT ts, source, price
FROM samples
WHERE ts > $1 AND ts < $2 AND ts < $3
ORDER BY ts, source
`, wStart, wEnd, horizon)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var out []twap.Sample
	for rows.Next() {
		var s twap.Sample
		if err := rows.Scan(&s.TS, &s.Source, &s.Price); err != nil {
			return nil, err
		}
		out = append(out, s)
	}
	return out, rows.Err()
}

// VersionRow is one stored window version.
type VersionRow struct {
	WindowStart   int64          `json:"window_start"`
	WindowEnd     int64          `json:"window_end"`
	Version       int            `json:"version"`
	InputHash     string         `json:"input_hash"`
	Signature     string         `json:"signature"`
	TWAPNum       string         `json:"twap_num"`
	TWAPDen       string         `json:"twap_den"`
	CoveredMicros int64          `json:"covered_micros"`
	WindowMicros  int64          `json:"window_micros"`
	ConflictCount int            `json:"conflict_count"`
	Stale         bool           `json:"stale"`
	LastSampleTS  int64          `json:"last_sample_ts"`
	HasAnchor     bool           `json:"has_anchor"`
	SamplesUsed   int            `json:"samples_used"`
	Segments      []twap.Segment `json:"segments"`
	ComputedAt    int64          `json:"computed_at"`
}

// LatestVersion returns the highest-version row for a window.
func (d *DB) LatestVersion(ctx context.Context, wStart int64) (*VersionRow, error) {
	row := d.Pool.QueryRow(ctx, `
SELECT window_start, window_end, version, input_hash, signature,
       COALESCE(twap_num, ''), COALESCE(twap_den, ''),
       covered_micros, window_micros, conflict_count, stale,
       last_sample_ts, has_anchor, samples_used, segments, computed_at
FROM window_versions
WHERE window_start = $1
ORDER BY version DESC
LIMIT 1`, wStart)
	return scanVersion(row)
}

// GetVersion returns a specific version of a window.
func (d *DB) GetVersion(ctx context.Context, wStart int64, version int) (*VersionRow, error) {
	row := d.Pool.QueryRow(ctx, `
SELECT window_start, window_end, version, input_hash, signature,
       COALESCE(twap_num, ''), COALESCE(twap_den, ''),
       covered_micros, window_micros, conflict_count, stale,
       last_sample_ts, has_anchor, samples_used, segments, computed_at
FROM window_versions
WHERE window_start = $1 AND version = $2`, wStart, version)
	return scanVersion(row)
}

// ListWindowStarts returns starts of windows that have any version,
// newest first, limited.
func (d *DB) ListWindowStarts(ctx context.Context, limit int) ([]int64, error) {
	rows, err := d.Pool.Query(ctx,
		`SELECT window_start FROM window_versions
		 GROUP BY window_start ORDER BY window_start DESC LIMIT $1`, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []int64
	for rows.Next() {
		var v int64
		if err := rows.Scan(&v); err != nil {
			return nil, err
		}
		out = append(out, v)
	}
	return out, rows.Err()
}

// MinSampleTS returns the oldest sample timestamp.
func (d *DB) MinSampleTS(ctx context.Context) (int64, error) {
	var ts *int64
	err := d.Pool.QueryRow(ctx, `SELECT min(ts) FROM samples`).Scan(&ts)
	if err != nil {
		return 0, err
	}
	if ts == nil {
		return 0, ErrNotFound
	}
	return *ts, nil
}

func scanVersion(row pgx.Row) (*VersionRow, error) {
	var v VersionRow
	err := row.Scan(
		&v.WindowStart, &v.WindowEnd, &v.Version, &v.InputHash, &v.Signature,
		&v.TWAPNum, &v.TWAPDen,
		&v.CoveredMicros, &v.WindowMicros, &v.ConflictCount, &v.Stale,
		&v.LastSampleTS, &v.HasAnchor, &v.SamplesUsed, &v.Segments,
		&v.ComputedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, err
	}
	return &v, nil
}

// WithTx runs fn inside a transaction. Materialization serializes
// per-window with an advisory lock (see LockWindowTx), so the default
// READ COMMITTED isolation is sufficient.
func (d *DB) WithTx(ctx context.Context, fn func(pgx.Tx) error) error {
	tx, err := d.Pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	if err := fn(tx); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

// LockWindowTx takes a transaction-scoped exclusive advisory lock for a
// window start, so concurrent ingests that touch the same window
// serialize their read-compute-append sequences without relying on
// SERIALIZABLE abort/retry. Different windows stay concurrent.
func (d *DB) LockWindowTx(ctx context.Context, tx pgx.Tx, wStart int64) error {
	_, err := tx.Exec(ctx,
		`SELECT pg_advisory_xact_lock(hashtextextended($1, 0))`,
		fmt.Sprintf("twap-window:%d", wStart))
	return err
}

// LoadWindowSamplesTx is the transactional variant.
func (d *DB) LoadWindowSamplesTx(ctx context.Context, tx pgx.Tx, wStart, wEnd, horizon int64) ([]twap.Sample, error) {
	rows, err := tx.Query(ctx, `
SELECT ts, source, price FROM (
    SELECT DISTINCT ON (source) ts, source, price
    FROM samples
    WHERE ts <= $1
    ORDER BY source, ts DESC
) anchor
UNION ALL
SELECT ts, source, price
FROM samples
WHERE ts > $1 AND ts < $2 AND ts < $3
ORDER BY ts, source
`, wStart, wEnd, horizon)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []twap.Sample
	for rows.Next() {
		var s twap.Sample
		if err := rows.Scan(&s.TS, &s.Source, &s.Price); err != nil {
			return nil, err
		}
		out = append(out, s)
	}
	return out, rows.Err()
}

// LatestVersionHashTx returns the input hash of the latest stored
// version inside tx, or "" when no version exists.
func (d *DB) LatestVersionHashTx(ctx context.Context, tx pgx.Tx, wStart int64) (hash string, version int, err error) {
	err = tx.QueryRow(ctx,
		`SELECT input_hash, version FROM window_versions
		 WHERE window_start = $1 ORDER BY version DESC LIMIT 1`,
		wStart).Scan(&hash, &version)
	if errors.Is(err, pgx.ErrNoRows) {
		return "", 0, nil
	}
	return hash, version, err
}

// InsertVersionTx appends a new version row inside tx.
func (d *DB) InsertVersionTx(ctx context.Context, tx pgx.Tx, v *VersionRow, twapNum, twapDen *string) error {
	num, den := interface{}(nil), interface{}(nil)
	if twapNum != nil {
		num, den = *twapNum, *twapDen
	}
	_, err := tx.Exec(ctx, `
INSERT INTO window_versions
  (window_start, version, window_end, input_hash, signature,
   twap_num, twap_den, covered_micros, window_micros, conflict_count,
   stale, last_sample_ts, has_anchor, samples_used, segments, computed_at)
VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)`,
		v.WindowStart, v.Version, v.WindowEnd, v.InputHash, v.Signature,
		num, den, v.CoveredMicros, v.WindowMicros, v.ConflictCount,
		v.Stale, v.LastSampleTS, v.HasAnchor, v.SamplesUsed, v.Segments,
		v.ComputedAt)
	return err
}
