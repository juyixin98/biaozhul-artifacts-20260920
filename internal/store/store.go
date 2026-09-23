// Package store persists parsed fixture samples in SQLite and reads them
// back for analysis. Raw parsed values are stored; rates and events are
// computed on read so the analysis logic has a single implementation.
package store

import (
	"database/sql"
	"encoding/json"
	"fmt"

	_ "modernc.org/sqlite"

	"cgroup-analyzer/internal/cgroup"
	"cgroup-analyzer/internal/fixture"
)

type Store struct {
	db *sql.DB
}

const schema = `
CREATE TABLE IF NOT EXISTS instances (
    container   TEXT NOT NULL,
    instance    TEXT NOT NULL,
    exit_code   INTEGER,
    exit_reason TEXT,
    exit_time   INTEGER,
    PRIMARY KEY (container, instance)
);
CREATE TABLE IF NOT EXISTS samples (
    container        TEXT NOT NULL,
    instance         TEXT NOT NULL,
    seq              INTEGER NOT NULL,
    ts               INTEGER NOT NULL,
    cpu_usage_usec   INTEGER,
    cpu_user_usec    INTEGER,
    cpu_system_usec  INTEGER,
    nr_throttled     INTEGER,
    throttled_usec   INTEGER,
    mem_current      INTEGER,
    mem_max          INTEGER,
    mem_max_set      INTEGER NOT NULL DEFAULT 0,
    ev_low           INTEGER,
    ev_high          INTEGER,
    ev_max           INTEGER,
    ev_oom           INTEGER,
    ev_oom_kill      INTEGER,
    ev_oom_group_kill INTEGER,
    cpu_pressure     TEXT,
    mem_pressure     TEXT,
    warnings         TEXT,
    PRIMARY KEY (container, instance, seq)
);
`

// Open opens (and if needed creates) the SQLite database at dsn
// (e.g. "file:data.db" or ":memory:").
func Open(dsn string) (*Store, error) {
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, err
	}
	// SQLite allows one writer at a time; a single connection avoids
	// SQLITE_BUSY on concurrent API requests.
	db.SetMaxOpenConns(1)
	if _, err := db.Exec(schema); err != nil {
		db.Close()
		return nil, fmt.Errorf("create schema: %w", err)
	}
	return &Store{db: db}, nil
}

func (s *Store) Close() error { return s.db.Close() }

// ReplaceInstances atomically replaces all stored data with the given
// instances (re-ingest semantics).
func (s *Store) ReplaceInstances(insts []fixture.Instance) error {
	tx, err := s.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if _, err := tx.Exec("DELETE FROM samples"); err != nil {
		return err
	}
	if _, err := tx.Exec("DELETE FROM instances"); err != nil {
		return err
	}
	instStmt, err := tx.Prepare(`INSERT INTO instances
		(container, instance, exit_code, exit_reason, exit_time) VALUES (?,?,?,?,?)`)
	if err != nil {
		return err
	}
	defer instStmt.Close()
	smpStmt, err := tx.Prepare(`INSERT INTO samples
		(container, instance, seq, ts, cpu_usage_usec, cpu_user_usec, cpu_system_usec,
		 nr_throttled, throttled_usec, mem_current, mem_max, mem_max_set,
		 ev_low, ev_high, ev_max, ev_oom, ev_oom_kill, ev_oom_group_kill,
		 cpu_pressure, mem_pressure, warnings)
		VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`)
	if err != nil {
		return err
	}
	defer smpStmt.Close()

	for _, inst := range insts {
		var code, reason, ts any
		if inst.Exit != nil {
			code, reason, ts = inst.Exit.Code, inst.Exit.Reason, inst.Exit.Time
		}
		if _, err := instStmt.Exec(inst.Container, inst.Instance, code, reason, ts); err != nil {
			return fmt.Errorf("insert instance %s/%s: %w", inst.Container, inst.Instance, err)
		}
		for _, sm := range inst.Samples {
			args := []any{sm.Container, sm.Instance, sm.Seq, sm.Timestamp}
			args = append(args, cpuCols(sm.CPU)...)
			args = append(args, nullUint(sm.MemCurrent), nullUint(sm.MemMax), boolInt(sm.MemMaxSet))
			args = append(args, eventCols(sm.MemEvents)...)
			cp, err := jsonOrNull(sm.CPUPressure)
			if err != nil {
				return err
			}
			mp, err := jsonOrNull(sm.MemPressure)
			if err != nil {
				return err
			}
			warn, err := json.Marshal(sm.Warnings)
			if err != nil {
				return err
			}
			args = append(args, cp, mp, string(warn))
			if _, err := smpStmt.Exec(args...); err != nil {
				return fmt.Errorf("insert sample %s/%s/%06d: %w", sm.Container, sm.Instance, sm.Seq, err)
			}
		}
	}
	return tx.Commit()
}

func nullUint(p *uint64) any {
	if p == nil {
		return nil
	}
	return int64(*p)
}

func boolInt(b bool) int {
	if b {
		return 1
	}
	return 0
}

func cpuCols(c *cgroup.CPUStat) []any {
	if c == nil {
		return []any{nil, nil, nil, nil, nil}
	}
	return []any{int64(c.UsageUsec), int64(c.UserUsec), int64(c.SystemUsec),
		int64(c.NrThrottled), int64(c.ThrottledUsec)}
}

func eventCols(e *cgroup.MemoryEvents) []any {
	if e == nil {
		return []any{nil, nil, nil, nil, nil, nil}
	}
	return []any{int64(e.Low), int64(e.High), int64(e.Max), int64(e.Oom),
		int64(e.OomKill), int64(e.OomGroupKill)}
}

func jsonOrNull(p *cgroup.Pressure) (any, error) {
	if p == nil {
		return nil, nil
	}
	b, err := json.Marshal(p)
	if err != nil {
		return nil, err
	}
	return string(b), nil
}

// InstanceSummary is one row of the container listing.
type InstanceSummary struct {
	Container   string `json:"container"`
	Instance    string `json:"instance"`
	SampleCount int    `json:"sample_count"`
	FirstTime   int64  `json:"first_time"`
	LastTime    int64  `json:"last_time"`
	HasExit     bool   `json:"has_exit"`
}

// ListInstances returns all instances with basic stats.
func (s *Store) ListInstances() ([]InstanceSummary, error) {
	rows, err := s.db.Query(`
		SELECT i.container, i.instance, COUNT(s.seq), COALESCE(MIN(s.ts),0), COALESCE(MAX(s.ts),0),
		       i.exit_code IS NOT NULL
		FROM instances i LEFT JOIN samples s
		  ON s.container = i.container AND s.instance = i.instance
		GROUP BY i.container, i.instance
		ORDER BY i.container, i.instance`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []InstanceSummary
	for rows.Next() {
		var v InstanceSummary
		if err := rows.Scan(&v.Container, &v.Instance, &v.SampleCount, &v.FirstTime, &v.LastTime, &v.HasExit); err != nil {
			return nil, err
		}
		out = append(out, v)
	}
	return out, rows.Err()
}

// GetInstance loads one full instance (samples + exit record).
// found is false when the instance does not exist.
func (s *Store) GetInstance(container, instance string) (fixture.Instance, bool, error) {
	inst := fixture.Instance{Container: container, Instance: instance}
	var code, ts sql.NullInt64
	var reason sql.NullString
	err := s.db.QueryRow(`SELECT exit_code, exit_reason, exit_time FROM instances
		WHERE container = ? AND instance = ?`, container, instance).Scan(&code, &reason, &ts)
	if err == sql.ErrNoRows {
		return inst, false, nil
	}
	if err != nil {
		return inst, false, err
	}
	if code.Valid {
		inst.Exit = &fixture.ExitInfo{Code: int(code.Int64), Reason: reason.String, Time: ts.Int64}
	}

	rows, err := s.db.Query(`SELECT seq, ts,
		cpu_usage_usec, cpu_user_usec, cpu_system_usec, nr_throttled, throttled_usec,
		mem_current, mem_max, mem_max_set,
		ev_low, ev_high, ev_max, ev_oom, ev_oom_kill, ev_oom_group_kill,
		cpu_pressure, mem_pressure, warnings
		FROM samples WHERE container = ? AND instance = ? ORDER BY seq`, container, instance)
	if err != nil {
		return inst, false, err
	}
	defer rows.Close()
	for rows.Next() {
		var sm fixture.Sample
		sm.Container, sm.Instance = container, instance
		var cpu [5]sql.NullInt64
		var memCur, memMax sql.NullInt64
		var memMaxSet int
		var ev [6]sql.NullInt64
		var cp, mp, warn sql.NullString
		if err := rows.Scan(&sm.Seq, &sm.Timestamp,
			&cpu[0], &cpu[1], &cpu[2], &cpu[3], &cpu[4],
			&memCur, &memMax, &memMaxSet,
			&ev[0], &ev[1], &ev[2], &ev[3], &ev[4], &ev[5],
			&cp, &mp, &warn); err != nil {
			return inst, false, err
		}
		if cpu[0].Valid {
			sm.CPU = &cgroup.CPUStat{
				UsageUsec: uint64(cpu[0].Int64), UserUsec: uint64(cpu[1].Int64),
				SystemUsec: uint64(cpu[2].Int64), NrThrottled: uint64(cpu[3].Int64),
				ThrottledUsec: uint64(cpu[4].Int64),
			}
		}
		if memCur.Valid {
			v := uint64(memCur.Int64)
			sm.MemCurrent = &v
		}
		sm.MemMaxSet = memMaxSet != 0
		if memMax.Valid {
			v := uint64(memMax.Int64)
			sm.MemMax = &v
		}
		if ev[0].Valid {
			sm.MemEvents = &cgroup.MemoryEvents{
				Low: uint64(ev[0].Int64), High: uint64(ev[1].Int64), Max: uint64(ev[2].Int64),
				Oom: uint64(ev[3].Int64), OomKill: uint64(ev[4].Int64), OomGroupKill: uint64(ev[5].Int64),
			}
		}
		if cp.Valid {
			var p cgroup.Pressure
			if err := json.Unmarshal([]byte(cp.String), &p); err != nil {
				return inst, false, err
			}
			sm.CPUPressure = &p
		}
		if mp.Valid {
			var p cgroup.Pressure
			if err := json.Unmarshal([]byte(mp.String), &p); err != nil {
				return inst, false, err
			}
			sm.MemPressure = &p
		}
		if warn.Valid && warn.String != "" && warn.String != "null" {
			if err := json.Unmarshal([]byte(warn.String), &sm.Warnings); err != nil {
				return inst, false, err
			}
		}
		inst.Samples = append(inst.Samples, sm)
	}
	return inst, true, rows.Err()
}
