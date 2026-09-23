// Package store implements PostgreSQL persistence for samples, sources,
// replay-protection nonces and materialized TWAP window versions.
package store

import (
	"context"
	_ "embed"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"twap-service/internal/domain"
)

//go:embed schema.sql
var schemaSQL string

// ErrNotFound is returned for single-row lookups that miss.
var ErrNotFound = errors.New("not found")

// ConflictInfo describes an existing sample on the same (symbol, ts) but from
// a different source with a different price.
type ConflictInfo struct {
	SourceA string `json:"source_a"`
	SourceB string `json:"source_b"`
	TSUsec  int64  `json:"ts_us"`
	PriceA  int64  `json:"price_a"`
	PriceB  int64  `json:"price_b"`
}

// SameTSDuplicate describes an existing row at (symbol, ts).
type SameTSDuplicate struct {
	Source string
	Price  int64
}

type Store struct {
	pool *pgxpool.Pool
}

func New(ctx context.Context, dsn string) (*Store, error) {
	cfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		return nil, fmt.Errorf("parse dsn: %w", err)
	}
	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		return nil, fmt.Errorf("connect: %w", err)
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, fmt.Errorf("ping: %w", err)
	}
	s := &Store{pool: pool}
	if err := s.migrate(ctx); err != nil {
		pool.Close()
		return nil, err
	}
	return s, nil
}

func (s *Store) Close() { s.pool.Close() }

func (s *Store) migrate(ctx context.Context) error {
	if _, err := s.pool.Exec(ctx, schemaSQL); err != nil {
		return fmt.Errorf("apply schema: %w", err)
	}
	return nil
}

func (s *Store) Pool() *pgxpool.Pool { return s.pool }

// SourceRecord is a registered ingest source.
type SourceRecord struct {
	Name      string
	Priority  int
	SecretKey string // base64
}

func (s *Store) CreateSource(ctx context.Context, rec SourceRecord) error {
	_, err := s.pool.Exec(ctx,
		`INSERT INTO sources(name, priority, secret_key)
		 VALUES ($1,$2,$3)
		 ON CONFLICT (name) DO UPDATE
		   SET priority = EXCLUDED.priority, secret_key = EXCLUDED.secret_key`,
		rec.Name, rec.Priority, rec.SecretKey)
	return err
}

func (s *Store) GetSource(ctx context.Context, name string) (SourceRecord, error) {
	row := s.pool.QueryRow(ctx,
		`SELECT name, priority, secret_key FROM sources WHERE name=$1`, name)
	var rec SourceRecord
	if err := row.Scan(&rec.Name, &rec.Priority, &rec.SecretKey); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return rec, ErrNotFound
		}
		return rec, err
	}
	return rec, nil
}

func (s *Store) ListPriorities(ctx context.Context) (map[string]int, error) {
	rows, err := s.pool.Query(ctx, `SELECT name, priority FROM sources`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[string]int{}
	for rows.Next() {
		var n string
		var p int
		if err := rows.Scan(&n, &p); err != nil {
			return nil, err
		}
		out[n] = p
	}
	return out, rows.Err()
}

// ConsumeNonce records a nonce, returning false if it was already used.
// Old nonces are pruned opportunistically.
func (s *Store) ConsumeNonce(ctx context.Context, tx pgx.Tx, source, nonce string, tsUs, maxAgeUs int64) (bool, error) {
	if _, err := tx.Exec(ctx,
		`DELETE FROM used_nonces WHERE ts_us < $1`, tsUs-maxAgeUs); err != nil {
		return false, err
	}
	ct, err := tx.Exec(ctx,
		`INSERT INTO used_nonces(source, nonce, ts_us) VALUES ($1,$2,$3)
		 ON CONFLICT DO NOTHING`, source, nonce, tsUs)
	if err != nil {
		return false, err
	}
	return ct.RowsAffected() == 1, nil
}

// LoadContextRows returns the predecessor timestamp (0 when none), successor
// timestamp (0 when none) and every existing row at tsUs.
func (s *Store) LoadContextRows(ctx context.Context, tx pgx.Tx, symbol string, tsUs int64) (
	prevTs, nextTs int64, same []SameTSDuplicate, err error,
) {
	if rerr := tx.QueryRow(ctx,
		`SELECT COALESCE(MAX(ts_us),0) FROM samples WHERE symbol=$1 AND ts_us < $2`,
		symbol, tsUs).Scan(&prevTs); rerr != nil {
		return 0, 0, nil, rerr
	}
	if rerr := tx.QueryRow(ctx,
		`SELECT COALESCE(MIN(ts_us),0) FROM samples WHERE symbol=$1 AND ts_us > $2`,
		symbol, tsUs).Scan(&nextTs); rerr != nil {
		return 0, 0, nil, rerr
	}
	rows, rerr := tx.Query(ctx,
		`SELECT source, price FROM samples WHERE symbol=$1 AND ts_us=$2`,
		symbol, tsUs)
	if rerr != nil {
		return 0, 0, nil, rerr
	}
	defer rows.Close()
	for rows.Next() {
		var d SameTSDuplicate
		if rerr := rows.Scan(&d.Source, &d.Price); rerr != nil {
			return 0, 0, nil, rerr
		}
		same = append(same, d)
	}
	return prevTs, nextTs, same, rows.Err()
}

// FindConflict returns a conflicting different-price, different-source row at
// the exact same timestamp (nil when none).
func FindConflict(same []SameTSDuplicate, source string, price int64) *ConflictInfo {
	for _, d := range same {
		if d.Source != source && d.Price != price {
			return &ConflictInfo{
				SourceA: source, SourceB: d.Source,
				PriceA: price, PriceB: d.Price,
			}
		}
	}
	return nil
}

// UpsertSample inserts or replaces a sample from one source. It reports
// whether a row from this source existed and whether the price changed.
func (s *Store) UpsertSample(ctx context.Context, tx pgx.Tx, smp domain.Sample) (hadSelf, changed bool, err error) {
	var oldPrice *int64
	if err = tx.QueryRow(ctx,
		`SELECT price FROM samples
		 WHERE symbol=$1 AND ts_us=$2 AND source=$3`,
		smp.Symbol, smp.TS, smp.Source).Scan(&oldPrice); err != nil {
		if !errors.Is(err, pgx.ErrNoRows) {
			return false, false, err
		}
		oldPrice = nil
	}
	hadSelf = oldPrice != nil
	changed = oldPrice == nil || *oldPrice != smp.Price
	_, err = tx.Exec(ctx,
		`INSERT INTO samples(symbol, ts_us, source, price)
		 VALUES ($1,$2,$3,$4)
		 ON CONFLICT (symbol, ts_us, source)
		 DO UPDATE SET price = EXCLUDED.price`,
		smp.Symbol, smp.TS, smp.Source, smp.Price)
	return hadSelf, changed, err
}

// EventRow is one raw sample row used for window computation.
type EventRow struct {
	TSUsec int64
	Price  int64
	Source string
}

// LoadWindowInputs loads raw samples needed to compute one window, including
// the carry-in row (latest sample at or before window start) and the source
// priority map. Rows strictly at the end boundary are excluded (they belong to
// the next window); rows past asOf are excluded as not yet observable.
func (s *Store) LoadWindowInputs(ctx context.Context, tx pgx.Tx, symbol string, startUs, endUs, asOfUs int64) ([]EventRow, *EventRow, map[string]int, error) {
	priorities, err := s.ListPriorities(ctx)
	if err != nil {
		return nil, nil, nil, err
	}
	var carry *EventRow
	{
		var e EventRow
		rerr := tx.QueryRow(ctx,
			`SELECT ts_us, price, source FROM samples
			 WHERE symbol=$1 AND ts_us <= $2 AND ts_us <= $3
			 ORDER BY ts_us DESC LIMIT 1`,
			symbol, startUs, asOfUs).Scan(&e.TSUsec, &e.Price, &e.Source)
		switch {
		case rerr == nil:
			carry = &e
		case errors.Is(rerr, pgx.ErrNoRows):
			// no carry-in
		default:
			return nil, nil, nil, rerr
		}
	}
	rows, err := tx.Query(ctx,
		`SELECT ts_us, price, source FROM samples
		 WHERE symbol=$1 AND ts_us > $2 AND ts_us < $3 AND ts_us <= $4
		 ORDER BY ts_us ASC`,
		symbol, startUs, endUs, asOfUs)
	if err != nil {
		return nil, nil, nil, err
	}
	defer rows.Close()
	var evs []EventRow
	for rows.Next() {
		var e EventRow
		if err := rows.Scan(&e.TSUsec, &e.Price, &e.Source); err != nil {
			return nil, nil, nil, err
		}
		evs = append(evs, e)
	}
	return evs, carry, priorities, rows.Err()
}

// VersionRow is a persisted window version.
type VersionRow struct {
	WindowStartUs int64
	WindowSec     int64
	Version       int
	Integral      string
	CoveredUsec   int64
	WindowUsec    int64
	TWAPExact     string
	TWAP6         string
	Coverage6     string
	Stale         bool
	LastSampleUs  *int64
	Sources       []string
	Conflicts     []string
	ContentHash   string
	ComputedAt    time.Time
}

const versionColumns = `window_start_us, window_sec, version, integral::text,
	covered_usec, window_usec, twap_exact, twap6, coverage6, stale,
	last_sample_us, sources, conflicts, content_hash, computed_at`

func scanVersion(row pgx.Row) (*VersionRow, error) {
	var v VersionRow
	if err := row.Scan(&v.WindowStartUs, &v.WindowSec, &v.Version, &v.Integral,
		&v.CoveredUsec, &v.WindowUsec, &v.TWAPExact, &v.TWAP6, &v.Coverage6, &v.Stale,
		&v.LastSampleUs, &v.Sources, &v.Conflicts, &v.ContentHash, &v.ComputedAt); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, nil
		}
		return nil, err
	}
	return &v, nil
}

// GetLatestVersion returns the highest stored version for a window (nil if
// none exists).
func (s *Store) GetLatestVersion(ctx context.Context, tx pgx.Tx, symbol string, startUs int64) (*VersionRow, error) {
	row := tx.QueryRow(ctx,
		`SELECT `+versionColumns+`
		 FROM window_versions
		 WHERE symbol=$1 AND window_start_us=$2
		 ORDER BY version DESC LIMIT 1`, symbol, startUs)
	return scanVersion(row)
}

// NextVersionNo returns version+1 for the window (1 when no rows exist).
func (s *Store) NextVersionNo(ctx context.Context, tx pgx.Tx, symbol string, startUs int64) (int, error) {
	var n int
	if err := tx.QueryRow(ctx,
		`SELECT COALESCE(MAX(version),0)+1 FROM window_versions
		 WHERE symbol=$1 AND window_start_us=$2`, symbol, startUs).Scan(&n); err != nil {
		return 0, err
	}
	return n, nil
}

// InsertVersion persists a new window version.
func (s *Store) InsertVersion(ctx context.Context, tx pgx.Tx, symbol string, v VersionRow) error {
	_, err := tx.Exec(ctx,
		`INSERT INTO window_versions(
		   symbol, window_start_us, window_sec, version, integral,
		   covered_usec, window_usec, twap_exact, twap6, coverage6, stale,
		   last_sample_us, sources, conflicts, content_hash)
		 VALUES ($1,$2,$3,$4,$5::numeric, $6,$7,$8,$9,$10,$11,
		         $12,$13,$14,$15)`,
		symbol, v.WindowStartUs, v.WindowSec, v.Version, v.Integral,
		v.CoveredUsec, v.WindowUsec, v.TWAPExact, v.TWAP6, v.Coverage6, v.Stale,
		v.LastSampleUs, v.Sources, v.Conflicts, v.ContentHash)
	return err
}

// ClearWindowVersions removes all stored versions (used by full rebuild).
func (s *Store) ClearWindowVersions(ctx context.Context, tx pgx.Tx) error {
	_, err := tx.Exec(ctx, `TRUNCATE window_versions`)
	return err
}

// ClearSymbolVersions removes all stored versions of one symbol.
func (s *Store) ClearSymbolVersions(ctx context.Context, tx pgx.Tx, symbol string) error {
	_, err := tx.Exec(ctx, `DELETE FROM window_versions WHERE symbol=$1`, symbol)
	return err
}

// SampleTsRange returns the min/max sample timestamp (0 when empty).
func (s *Store) SampleTsRange(ctx context.Context, symbol string) (minUs, maxUs int64, err error) {
	err = s.pool.QueryRow(ctx,
		`SELECT COALESCE(MIN(ts_us),0), COALESCE(MAX(ts_us),0)
		 FROM samples WHERE symbol=$1`, symbol).Scan(&minUs, &maxUs)
	return
}

// DistinctSymbols lists symbols that have at least one sample.
func (s *Store) DistinctSymbols(ctx context.Context) ([]string, error) {
	rows, err := s.pool.Query(ctx, `SELECT DISTINCT symbol FROM samples ORDER BY 1`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var sym string
		if err := rows.Scan(&sym); err != nil {
			return nil, err
		}
		out = append(out, sym)
	}
	return out, rows.Err()
}

// ListWindowLatest returns the latest persisted version of every window in the
// microsecond range [fromUs, toUs) for a symbol, ordered by start.
func (s *Store) ListWindowLatest(ctx context.Context, symbol string, fromUs, toUs int64) ([]VersionRow, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT DISTINCT ON (window_start_us) `+versionColumns+`
		 FROM window_versions
		 WHERE symbol=$1 AND window_start_us >= $2 AND window_start_us < $3
		 ORDER BY window_start_us, version DESC`,
		symbol, fromUs, toUs)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []VersionRow
	for rows.Next() {
		v, err := scanVersion(rows)
		if err != nil {
			return nil, err
		}
		if v != nil {
			out = append(out, *v)
		}
	}
	return out, rows.Err()
}
