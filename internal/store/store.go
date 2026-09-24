// Package store persists ingest runs and analysis results in SQLite.
// All writes for one ingest run happen in a single transaction; readers query
// the newest run or an explicit run id.
package store

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	_ "modernc.org/sqlite"

	"cresnap/pkg/cganalyze"
	"cresnap/pkg/cgsample"
)

// Store wraps the SQLite database.
type Store struct{ db *sql.DB }

// Open opens (creating the schema in) the database file at dsn.
func Open(dsn string) (*Store, error) {
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(1) // SQLite: serialize writers/readers simply and safely
	if _, err := db.Exec(pragma); err != nil {
		db.Close()
		return nil, fmt.Errorf("pragma: %w", err)
	}
	if _, err := db.Exec(schema); err != nil {
		db.Close()
		return nil, fmt.Errorf("schema: %w", err)
	}
	return &Store{db: db}, nil
}

// Close releases the database handle.
func (s *Store) Close() error { return s.db.Close() }

const pragma = `
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;
PRAGMA synchronous=NORMAL;`

const schema = `
CREATE TABLE IF NOT EXISTS ingest_runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at       TEXT NOT NULL,
    finished_at      TEXT NOT NULL,
    fixture_root     TEXT NOT NULL,
    containers       INTEGER NOT NULL,
    samples_loaded   INTEGER NOT NULL,
    samples_ignored  INTEGER NOT NULL,
    load_errors_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS samples (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER NOT NULL REFERENCES ingest_runs(id) ON DELETE CASCADE,
    container       TEXT NOT NULL,
    seq             INTEGER NOT NULL,
    sample_dir      TEXT NOT NULL,
    ts              TEXT NOT NULL,
    instance_id     TEXT,
    cpu_usage_usec  INTEGER NOT NULL,
    memory_current  INTEGER NOT NULL,
    payload_json    TEXT NOT NULL,
    UNIQUE(run_id, container, seq)
);
CREATE TABLE IF NOT EXISTS intervals (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            INTEGER NOT NULL REFERENCES ingest_runs(id) ON DELETE CASCADE,
    container         TEXT NOT NULL,
    seq               INTEGER NOT NULL,
    from_dir          TEXT NOT NULL,
    to_dir            TEXT NOT NULL,
    from_ts           TEXT NOT NULL,
    to_ts             TEXT NOT NULL,
    wall_seconds      REAL NOT NULL,
    status            TEXT NOT NULL,
    missing_samples   INTEGER NOT NULL,
    reset_counters    TEXT NOT NULL,
    instance_from     TEXT,
    instance_to       TEXT,
    payload_json      TEXT NOT NULL,
    UNIQUE(run_id, container, seq)
);
CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER NOT NULL REFERENCES ingest_runs(id) ON DELETE CASCADE,
    container     TEXT NOT NULL,
    seq           INTEGER NOT NULL,
    kind          TEXT NOT NULL,
    at            TEXT NOT NULL,
    summary       TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    sources_json  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_intervals_run ON intervals(run_id, container);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, container, kind);
CREATE INDEX IF NOT EXISTS idx_samples_run ON samples(run_id, container);`

// RunSummary is the stored outcome of one POST /v1/ingest.
type RunSummary struct {
	ID             int64                  `json:"run_id"`
	StartedAt      string                 `json:"started_at"`
	FinishedAt     string                 `json:"finished_at"`
	FixtureRoot    string                 `json:"fixture_root"`
	Containers     int                    `json:"containers"`
	SamplesLoaded  int                    `json:"samples_loaded"`
	SamplesIgnored int                    `json:"samples_ignored"`
	LoadErrors     []cgsample.LoadError   `json:"load_errors"`
	IntervalCount  int                    `json:"interval_count"`
	EventCount     int                    `json:"event_count"`
}

// Ingest atomically stores one analyzed report and returns its run summary.
func (s *Store) Ingest(root string, rep *cganalyze.Report) (*RunSummary, error) {
	started := time.Now().UTC().Format(time.RFC3339Nano)
	tx, err := s.db.Begin()
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()

	errJSON, _ := json.Marshal(rep.LoadErrors)
	res, err := tx.Exec(
		`INSERT INTO ingest_runs(started_at, finished_at, fixture_root, containers, samples_loaded, samples_ignored, load_errors_json)
		 VALUES(?,?,?,?,?,?,?)`,
		started, started, root, len(rep.Containers), totalSamples(rep), rep.IgnoredSamples, string(errJSON))
	if err != nil {
		return nil, err
	}
	runID, err := res.LastInsertId()
	if err != nil {
		return nil, err
	}

	nIntervals, nEvents := 0, 0
	for _, cr := range rep.Containers {
		// Store every sample (seq 0-based) so the first/baseline sample is
		// queryable even though it has no interval.
		// The samples themselves live in the report's interval endpoints; we
		// reconstruct from a sample list passed separately via SamplePayload.
		for _, sp := range cr.SamplePayloads {
			pj, _ := json.Marshal(sp)
			var instance sql.NullString
			if sp.InstanceID != nil {
				instance = sql.NullString{String: *sp.InstanceID, Valid: true}
			}
			_, err = tx.Exec(`INSERT INTO samples
			    (run_id, container, seq, sample_dir, ts, instance_id, cpu_usage_usec, memory_current, payload_json)
				VALUES(?,?,?,?,?,?,?,?,?)`,
				runID, cr.Container, sp.Seq, sp.DirName, sp.Timestamp,
				instance, sp.CPUUsageUsec, sp.MemoryCurrent, string(pj))
			if err != nil {
				return nil, err
			}
		}
		for seq, iv := range cr.Intervals {
			pj, _ := json.Marshal(iv)
			_, err = tx.Exec(`INSERT INTO intervals
			    (run_id, container, seq, from_dir, to_dir, from_ts, to_ts, wall_seconds,
			     status, missing_samples, reset_counters, instance_from, instance_to, payload_json)
				VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
				runID, cr.Container, seq, iv.FromDir, iv.ToDir, iv.From, iv.To, iv.WallSeconds,
				iv.Status, iv.MissingSamples, strings.Join(iv.ResetCounters, ","),
				nullableStr(iv.InstanceFrom), nullableStr(iv.InstanceTo), string(pj))
			if err != nil {
				return nil, err
			}
			nIntervals++
			for _, ev := range iv.Events {
				ej, _ := json.Marshal(ev.Evidence)
				sj, _ := json.Marshal(ev.Sources)
				_, err = tx.Exec(`INSERT INTO events
				    (run_id, container, seq, kind, at, summary, evidence_json, sources_json)
					VALUES(?,?,?,?,?,?,?,?)`,
					runID, cr.Container, seq, ev.Kind, ev.At, ev.Summary, string(ej), string(sj))
				if err != nil {
					return nil, err
				}
				nEvents++
			}
		}
	}
	finished := time.Now().UTC().Format(time.RFC3339Nano)
	if _, err := tx.Exec(`UPDATE ingest_runs SET finished_at=? WHERE id=?`, finished, runID); err != nil {
		return nil, err
	}
	if err := tx.Commit(); err != nil {
		return nil, err
	}
	return &RunSummary{
		ID: runID, StartedAt: started, FinishedAt: finished, FixtureRoot: root,
		Containers: len(rep.Containers), SamplesLoaded: totalSamples(rep),
		SamplesIgnored: rep.IgnoredSamples, LoadErrors: rep.LoadErrors,
		IntervalCount: nIntervals, EventCount: nEvents,
	}, nil
}

func totalSamples(rep *cganalyze.Report) int {
	n := 0
	for _, cr := range rep.Containers {
		n += len(cr.SamplePayloads)
	}
	return n
}

func nullableStr(p *string) interface{} {
	if p == nil {
		return nil
	}
	return *p
}

// LatestRunID returns the newest ingest run id, or 0 if none exists.
func (s *Store) LatestRunID() (int64, error) {
	var id int64
	err := s.db.QueryRow(`SELECT COALESCE(MAX(id),0) FROM ingest_runs`).Scan(&id)
	return id, err
}

// RunExists reports whether run id exists.
func (s *Store) RunExists(id int64) (bool, error) {
	var n int
	err := s.db.QueryRow(`SELECT COUNT(1) FROM ingest_runs WHERE id=?`, id).Scan(&n)
	return n > 0, err
}

// RunRow is one row of the ingest history.
type RunRow struct {
	ID            int64  `json:"run_id"`
	StartedAt     string `json:"started_at"`
	FinishedAt    string `json:"finished_at"`
	FixtureRoot   string `json:"fixture_root"`
	Containers    int    `json:"containers"`
	SamplesLoaded int    `json:"samples_loaded"`
	SamplesIgnored int   `json:"samples_ignored"`
	Intervals     int    `json:"interval_count"`
	Events        int    `json:"event_count"`
}

// ListRuns returns all ingest runs newest-first.
func (s *Store) ListRuns() ([]RunRow, error) {
	rows, err := s.db.Query(`
		SELECT r.id, r.started_at, r.finished_at, r.fixture_root, r.containers,
		       r.samples_loaded, r.samples_ignored,
		       (SELECT COUNT(*) FROM intervals i WHERE i.run_id=r.id),
		       (SELECT COUNT(*) FROM events e WHERE e.run_id=r.id)
		FROM ingest_runs r ORDER BY r.id DESC`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []RunRow
	for rows.Next() {
		var r RunRow
		if err := rows.Scan(&r.ID, &r.StartedAt, &r.FinishedAt, &r.FixtureRoot,
			&r.Containers, &r.SamplesLoaded, &r.SamplesIgnored, &r.Intervals, &r.Events); err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

// IntervalJSON returns one interval payload (raw JSON) or sql.ErrNoRows.
func (s *Store) IntervalJSON(runID int64, container string, seq int) ([]byte, error) {
	var pj string
	err := s.db.QueryRow(`SELECT payload_json FROM intervals WHERE run_id=? AND container=? AND seq=?`,
		runID, container, seq).Scan(&pj)
	if err != nil {
		return nil, err
	}
	return []byte(pj), nil
}

// IntervalSeqList maps to_dir -> seq for a container in a run.
func (s *Store) IntervalSeqList(runID int64, container string) ([]map[string]interface{}, error) {
	rows, err := s.db.Query(`SELECT seq, from_dir, to_dir, status FROM intervals
		WHERE run_id=? AND container=? ORDER BY seq`, runID, container)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []map[string]interface{}
	for rows.Next() {
		var seq, missing int
		var fromDir, toDir, status string
		if err := rows.Scan(&seq, &fromDir, &toDir, &status); err != nil {
			return nil, err
		}
		_ = missing
		out = append(out, map[string]interface{}{
			"seq": seq, "from_sample_dir": fromDir, "to_sample_dir": toDir, "status": status,
		})
	}
	return out, rows.Err()
}

// ContainerExists checks the container in a run.
func (s *Store) ContainerExists(runID int64, container string) (bool, error) {
	var n int
	err := s.db.QueryRow(`SELECT COUNT(1) FROM samples WHERE run_id=? AND container=?`,
		runID, container).Scan(&n)
	return n > 0, err
}

// SampleJSON returns one sample payload (raw JSON) or sql.ErrNoRows.
func (s *Store) SampleJSON(runID int64, container string, dir string) ([]byte, error) {
	var pj string
	err := s.db.QueryRow(`SELECT payload_json FROM samples WHERE run_id=? AND container=? AND sample_dir=?`,
		runID, container, dir).Scan(&pj)
	if err != nil {
		return nil, err
	}
	return []byte(pj), nil
}

// SampleList returns sample dirs/ids for a container.
func (s *Store) SampleList(runID int64, container string) ([]map[string]interface{}, error) {
	rows, err := s.db.Query(`SELECT seq, sample_dir, ts, instance_id FROM samples
		WHERE run_id=? AND container=? ORDER BY seq`, runID, container)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []map[string]interface{}
	for rows.Next() {
		var seq int
		var dir, ts string
		var inst sql.NullString
		if err := rows.Scan(&seq, &dir, &ts, &inst); err != nil {
			return nil, err
		}
		m := map[string]interface{}{"seq": seq, "sample_dir": dir, "timestamp": ts}
		if inst.Valid {
			m["instance_id"] = inst.String
		}
		out = append(out, m)
	}
	return out, rows.Err()
}

// EventRow is a stored event with raw evidence/sources JSON.
type EventRow struct {
	Seq       int             `json:"seq"`
	Container string          `json:"container"`
	Kind      string          `json:"kind"`
	At        string          `json:"at"`
	Summary   string          `json:"summary"`
	Evidence  json.RawMessage `json:"evidence"`
	Sources   json.RawMessage `json:"sources"`
}

// QueryEvents returns events filtered by run/container/kind (empty = all).
func (s *Store) QueryEvents(runID int64, container, kind string) ([]EventRow, error) {
	q := `SELECT container, seq, kind, at, summary, evidence_json, sources_json
	      FROM events WHERE run_id=?`
	args := []interface{}{runID}
	if container != "" {
		q += ` AND container=?`
		args = append(args, container)
	}
	if kind != "" {
		q += ` AND kind=?`
		args = append(args, kind)
	}
	q += ` ORDER BY id`
	rows, err := s.db.Query(q, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []EventRow
	for rows.Next() {
		var e EventRow
		var ej, sj string
		if err := rows.Scan(&e.Container, &e.Seq, &e.Kind, &e.At, &e.Summary, &ej, &sj); err != nil {
			return nil, err
		}
		e.Evidence = json.RawMessage(ej)
		e.Sources = json.RawMessage(sj)
		out = append(out, e)
	}
	return out, rows.Err()
}

// ContainerSummary is stored aggregate metadata for one container.
type ContainerSummary struct {
	Container      string  `json:"container"`
	SampleCount    int     `json:"sample_count"`
	NominalSeconds float64 `json:"nominal_seconds"`
	FirstSample    string  `json:"first_sample_dir"`
	LastSample     string  `json:"last_sample_dir"`
}

// ListContainers returns per-container metadata for a run.
func (s *Store) ListContainers(runID int64) ([]ContainerSummary, error) {
	rows, err := s.db.Query(`
		SELECT container, COUNT(*), MIN(ts), MAX(ts)
		FROM samples WHERE run_id=? GROUP BY container ORDER BY container`, runID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []ContainerSummary
	for rows.Next() {
		var c ContainerSummary
		if err := rows.Scan(&c.Container, &c.SampleCount, &c.FirstSample, &c.LastSample); err != nil {
			return nil, err
		}
		out = append(out, c)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	// Nominal cadence is computed during analysis; approximate from intervals
	// via MIN(wall_seconds) with status ok/gap not required.
	for i := range out {
		_ = s.db.QueryRow(`SELECT COALESCE(MIN(wall_seconds),0) FROM intervals WHERE run_id=? AND container=?`,
			runID, out[i].Container).Scan(&out[i].NominalSeconds)
	}
	return out, nil
}
