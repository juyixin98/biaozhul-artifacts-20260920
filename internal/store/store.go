// Package store is the SQLite persistence layer. All timestamps are stored
// as RFC3339Nano UTC strings; sequence gaps and optional values use NULL.
package store

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"strings"
	"time"

	_ "modernc.org/sqlite"

	"sensorhealth/internal/model"
)

// Store wraps one SQLite database. MaxOpenConns is 1: the engine
// serializes writes anyway and a single connection avoids SQLITE_BUSY.
type Store struct {
	db *sql.DB
}

const schema = `
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS device_types (
  type               TEXT PRIMARY KEY,
  stale_enter_sec    INTEGER NOT NULL,
  stale_recover_sec  INTEGER NOT NULL,
  fixed_window_count INTEGER NOT NULL,
  fixed_tolerance    REAL NOT NULL,
  gap_recover_gapless INTEGER NOT NULL,
  clock_skew_tol_ms  INTEGER NOT NULL,
  version            INTEGER NOT NULL,
  updated_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
  id             TEXT PRIMARY KEY,
  type           TEXT NOT NULL,
  epoch          INTEGER NOT NULL DEFAULT 0,
  has_seq        INTEGER NOT NULL DEFAULT 0,
  last_seq       INTEGER,
  last_sample    TEXT,
  last_heartbeat TEXT,
  last_recv_at   TEXT NOT NULL,
  created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id    TEXT NOT NULL,
  type         TEXT NOT NULL,
  seq          INTEGER,
  sample_time  TEXT NOT NULL,
  recv_at      TEXT NOT NULL,
  value        REAL,
  is_heartbeat INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_device ON messages(device_id, id);

CREATE TABLE IF NOT EXISTS device_states (
  device_id  TEXT PRIMARY KEY,
  data       TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
  id                     INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id              TEXT NOT NULL,
  device_type            TEXT NOT NULL,
  rule                   TEXT NOT NULL,
  open                   INTEGER NOT NULL,
  triggered_at           TEXT NOT NULL,
  config_version         INTEGER NOT NULL,
  recovered_at           TEXT,
  recovery_config_version INTEGER,
  start_seq              INTEGER,
  end_seq                INTEGER,
  start_sample           TEXT,
  end_sample             TEXT,
  gap_start              INTEGER,
  gap_end                INTEGER,
  note                   TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_device_rule_open ON events(device_id, rule, open);
CREATE INDEX IF NOT EXISTS idx_events_open ON events(open);

CREATE TABLE IF NOT EXISTS webhook_deliveries (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id    INTEGER,
  phase       TEXT NOT NULL,
  url         TEXT,
  ok          INTEGER NOT NULL,
  status_code INTEGER,
  error       TEXT,
  attempt     INTEGER NOT NULL,
  created_at  TEXT NOT NULL
);
`

// Open opens (creating if needed) the SQLite database at path and ensures
// the schema and seed configuration exist.
func Open(ctx context.Context, path string) (*Store, error) {
	dsn := fmt.Sprintf("file:%s?_pragma=busy_timeout(5000)&_pragma=journal_mode(WAL)&_pragma=foreign_keys(ON)", path)
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(1)
	if _, err := db.ExecContext(ctx, schema); err != nil {
		db.Close()
		return nil, fmt.Errorf("create schema: %w", err)
	}
	s := &Store{db: db}
	if err := s.seed(ctx); err != nil {
		db.Close()
		return nil, err
	}
	return s, nil
}

// Close releases the database handle.
func (s *Store) Close() error { return s.db.Close() }

func (s *Store) seed(ctx context.Context) error {
	var n int
	if err := s.db.QueryRowContext(ctx, `SELECT COUNT(*) FROM device_types`).Scan(&n); err != nil {
		return err
	}
	if n > 0 {
		return nil
	}
	now := time.Now().UTC()
	if _, err := s.db.ExecContext(ctx,
		`INSERT OR IGNORE INTO meta(key, value) VALUES('global_version','1')`); err != nil {
		return err
	}
	seed := []model.DeviceType{
		// Moving sensor: constant reading for 60 consecutive samples is
		// suspicious, 30s of silence is a stall.
		{Type: "temperature", StaleEnterSec: 30, StaleRecoverSec: 10,
			FixedWindowCount: 60, FixedTolerance: 0, GapRecoverGapless: 5,
			ClockSkewTolMs: 500, Version: 1, UpdatedAt: now},
		// Binary contact: being stationary is normal, so fixed-value
		// detection is disabled explicitly.
		{Type: "door", StaleEnterSec: 120, StaleRecoverSec: 30,
			FixedWindowCount: 0, FixedTolerance: 0, GapRecoverGapless: 5,
			ClockSkewTolMs: 500, Version: 1, UpdatedAt: now},
		// Switch: same story — 0/1 values legitimately stay constant.
		{Type: "switch", StaleEnterSec: 60, StaleRecoverSec: 20,
			FixedWindowCount: 0, FixedTolerance: 0, GapRecoverGapless: 5,
			ClockSkewTolMs: 500, Version: 1, UpdatedAt: now},
	}
	for _, dt := range seed {
		if err := s.insertType(ctx, dt); err != nil {
			return err
		}
	}
	return nil
}

func (s *Store) insertType(ctx context.Context, d model.DeviceType) error {
	_, err := s.db.ExecContext(ctx, `
INSERT INTO device_types(type, stale_enter_sec, stale_recover_sec,
  fixed_window_count, fixed_tolerance, gap_recover_gapless,
  clock_skew_tol_ms, version, updated_at)
VALUES(?,?,?,?,?,?,?,?,?)`,
		d.Type, d.StaleEnterSec, d.StaleRecoverSec, d.FixedWindowCount,
		d.FixedTolerance, d.GapRecoverGapless, d.ClockSkewTolMs, d.Version,
		d.UpdatedAt.Format(time.RFC3339Nano))
	return err
}

// RunTx executes fn inside a serializable transaction, committing on nil
// error and rolling back otherwise.
func (s *Store) RunTx(ctx context.Context, fn func(*sql.Tx) error) error {
	tx, err := s.db.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelSerializable})
	if err != nil {
		return err
	}
	if err := fn(tx); err != nil {
		_ = tx.Rollback()
		return err
	}
	return tx.Commit()
}

// ---------- time / nullable helpers ----------

func ts(t time.Time) string { return t.Format(time.RFC3339Nano) }

func nts(t *time.Time) sql.NullString {
	if t == nil {
		return sql.NullString{}
	}
	return sql.NullString{String: t.Format(time.RFC3339Nano), Valid: true}
}

func pts(ns sql.NullString) (*time.Time, error) {
	if !ns.Valid {
		return nil, nil
	}
	t, err := time.Parse(time.RFC3339Nano, ns.String)
	if err != nil {
		return nil, err
	}
	return &t, nil
}

func mustPTS(ns sql.NullString) (time.Time, error) {
	p, err := pts(ns)
	if err != nil || p == nil {
		return time.Time{}, err
	}
	return *p, nil
}

func ni64(v *int64) sql.NullInt64 {
	if v == nil {
		return sql.NullInt64{}
	}
	return sql.NullInt64{Int64: *v, Valid: true}
}

func pni64(v sql.NullInt64) *int64 {
	if !v.Valid {
		return nil
	}
	return &v.Int64
}

func nf64(v *float64) sql.NullFloat64 {
	if v == nil {
		return sql.NullFloat64{}
	}
	return sql.NullFloat64{Float64: *v, Valid: true}
}

// ---------- configuration ----------

// GlobalConfigVersion returns the monotonically increasing configuration
// version bumped on every device-type write.
func (s *Store) GlobalConfigVersion(ctx context.Context) (int, error) {
	var v sql.NullString
	if err := s.db.QueryRowContext(ctx,
		`SELECT value FROM meta WHERE key='global_version'`).Scan(&v); err != nil {
		return 0, err
	}
	if !v.Valid {
		return 1, nil
	}
	var n int
	if _, err := fmt.Sscanf(v.String, "%d", &n); err != nil {
		return 0, err
	}
	return n, nil
}

// UpsertDeviceType creates or replaces a device type, stamping it with a
// fresh global configuration version.
func (s *Store) UpsertDeviceType(ctx context.Context, d model.DeviceType) (model.DeviceType, error) {
	d.Type = strings.TrimSpace(d.Type)
	if err := d.Validate(); err != nil {
		return d, err
	}
	d.UpdatedAt = time.Now().UTC()
	err := s.RunTx(ctx, func(tx *sql.Tx) error {
		var cur sql.NullInt64
		if err := tx.QueryRowContext(ctx,
			`SELECT version FROM device_types WHERE type=?`, d.Type).Scan(&cur); err != nil {
			if !errors.Is(err, sql.ErrNoRows) {
				return err
			}
		}
		var gv int
		if err := tx.QueryRowContext(ctx,
			`SELECT CAST(value AS INTEGER) FROM meta WHERE key='global_version'`).Scan(&gv); err != nil {
			return err
		}
		d.Version = gv + 1
		if _, err := tx.ExecContext(ctx,
			`INSERT INTO meta(key,value) VALUES('global_version', ?)
			 ON CONFLICT(key) DO UPDATE SET value=excluded.value`,
			fmt.Sprintf("%d", d.Version)); err != nil {
			return err
		}
		if cur.Valid {
			_, err := tx.ExecContext(ctx, `
UPDATE device_types SET stale_enter_sec=?, stale_recover_sec=?,
  fixed_window_count=?, fixed_tolerance=?, gap_recover_gapless=?,
  clock_skew_tol_ms=?, version=?, updated_at=? WHERE type=?`,
				d.StaleEnterSec, d.StaleRecoverSec, d.FixedWindowCount,
				d.FixedTolerance, d.GapRecoverGapless, d.ClockSkewTolMs,
				d.Version, ts(d.UpdatedAt), d.Type)
			return err
		}
		return insertTypeTx(ctx, tx, d)
	})
	return d, err
}

func insertTypeTx(ctx context.Context, tx *sql.Tx, d model.DeviceType) error {
	_, err := tx.ExecContext(ctx, `
INSERT INTO device_types(type, stale_enter_sec, stale_recover_sec,
  fixed_window_count, fixed_tolerance, gap_recover_gapless,
  clock_skew_tol_ms, version, updated_at)
VALUES(?,?,?,?,?,?,?,?,?)`,
		d.Type, d.StaleEnterSec, d.StaleRecoverSec, d.FixedWindowCount,
		d.FixedTolerance, d.GapRecoverGapless, d.ClockSkewTolMs, d.Version,
		ts(d.UpdatedAt))
	return err
}

// GetDeviceType loads one type configuration.
func (s *Store) GetDeviceType(ctx context.Context, typ string) (model.DeviceType, error) {
	return scanType(s.db.QueryRowContext(ctx,
		`SELECT type, stale_enter_sec, stale_recover_sec, fixed_window_count,
		        fixed_tolerance, gap_recover_gapless, clock_skew_tol_ms,
		        version, updated_at FROM device_types WHERE type=?`, typ))
}

func scanType(row interface {
	Scan(dest ...any) error
}) (model.DeviceType, error) {
	var d model.DeviceType
	var up string
	err := row.Scan(&d.Type, &d.StaleEnterSec, &d.StaleRecoverSec,
		&d.FixedWindowCount, &d.FixedTolerance, &d.GapRecoverGapless,
		&d.ClockSkewTolMs, &d.Version, &up)
	if err != nil {
		return d, err
	}
	d.UpdatedAt, _ = time.Parse(time.RFC3339Nano, up)
	return d, nil
}

// ListDeviceTypes lists all configured device types.
func (s *Store) ListDeviceTypes(ctx context.Context) ([]model.DeviceType, error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT type, stale_enter_sec, stale_recover_sec, fixed_window_count,
		        fixed_tolerance, gap_recover_gapless, clock_skew_tol_ms,
		        version, updated_at FROM device_types ORDER BY type`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []model.DeviceType
	for rows.Next() {
		d, err := scanType(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, d)
	}
	return out, rows.Err()
}

// ---------- devices ----------

const deviceCols = `id, type, epoch, has_seq, last_seq, last_sample,
  last_heartbeat, last_recv_at, created_at`

func scanDevice(row interface {
	Scan(dest ...any) error
}) (model.Device, error) {
	var d model.Device
	var hasSeq, lastSeq sql.NullInt64
	var lastSample, lastHeartbeat sql.NullString
	var lastRecv, created string
	err := row.Scan(&d.ID, &d.Type, &d.Epoch, &hasSeq, &lastSeq,
		&lastSample, &lastHeartbeat, &lastRecv, &created)
	if err != nil {
		return d, err
	}
	d.HasSeq = hasSeq.Valid && hasSeq.Int64 != 0
	if lastSeq.Valid {
		d.LastSeq = lastSeq.Int64
	}
	p, err := pts(lastSample)
	if err != nil {
		return d, err
	}
	d.LastSample = p
	p, err = pts(lastHeartbeat)
	if err != nil {
		return d, err
	}
	d.LastHeartbeat = p
	d.LastRecvAt, err = time.Parse(time.RFC3339Nano, lastRecv)
	if err != nil {
		return d, err
	}
	d.CreatedAt, err = time.Parse(time.RFC3339Nano, created)
	return d, err
}

// GetDevice loads a device or returns sql.ErrNoRows.
func (s *Store) GetDevice(ctx context.Context, id string) (model.Device, error) {
	return scanDevice(s.db.QueryRowContext(ctx,
		`SELECT `+deviceCols+` FROM devices WHERE id=?`, id))
}

// GetDeviceTx is the transaction-scoped variant; callers inside RunTx must
// use it (the pool is a single connection, so querying through s.db from a
// tx would deadlock).
func GetDeviceTx(ctx context.Context, tx *sql.Tx, id string) (model.Device, error) {
	return scanDevice(tx.QueryRowContext(ctx,
		`SELECT `+deviceCols+` FROM devices WHERE id=?`, id))
}

// GetDeviceTypeTx is the transaction-scoped variant of GetDeviceType.
func GetDeviceTypeTx(ctx context.Context, tx *sql.Tx, typ string) (model.DeviceType, error) {
	return scanType(tx.QueryRowContext(ctx,
		`SELECT type, stale_enter_sec, stale_recover_sec, fixed_window_count,
		        fixed_tolerance, gap_recover_gapless, clock_skew_tol_ms,
		        version, updated_at FROM device_types WHERE type=?`, typ))
}

// CreateDeviceTx inserts a brand-new device inside tx.
func CreateDeviceTx(ctx context.Context, tx *sql.Tx, d *model.Device) error {
	hasSeq := 0
	if d.HasSeq {
		hasSeq = 1
	}
	_, err := tx.ExecContext(ctx, `
INSERT INTO devices(id,type,epoch,has_seq,last_seq,last_sample,last_heartbeat,
  last_recv_at,created_at) VALUES(?,?,?,?,?,?,?,?,?)`,
		d.ID, d.Type, d.Epoch, hasSeq, ni64(seqOrNil(d)), nts(d.LastSample),
		nts(d.LastHeartbeat), ts(d.LastRecvAt), ts(d.CreatedAt))
	return err
}

func seqOrNil(d *model.Device) *int64 {
	if d.HasSeq {
		v := d.LastSeq
		return &v
	}
	return nil
}

// UpsertDeviceTx writes the device projection inside tx.
func UpsertDeviceTx(ctx context.Context, tx *sql.Tx, d *model.Device) error {
	hasSeq := 0
	if d.HasSeq {
		hasSeq = 1
	}
	_, err := tx.ExecContext(ctx, `
INSERT INTO devices(id,type,epoch,has_seq,last_seq,last_sample,last_heartbeat,
  last_recv_at,created_at) VALUES(?,?,?,?,?,?,?,?,?)
ON CONFLICT(id) DO UPDATE SET
  type=excluded.type, epoch=excluded.epoch, has_seq=excluded.has_seq,
  last_seq=excluded.last_seq, last_sample=excluded.last_sample,
  last_heartbeat=excluded.last_heartbeat, last_recv_at=excluded.last_recv_at`,
		d.ID, d.Type, d.Epoch, hasSeq, ni64(seqOrNil(d)), nts(d.LastSample),
		nts(d.LastHeartbeat), ts(d.LastRecvAt), ts(d.CreatedAt))
	return err
}

// ListDevices returns all registered devices ordered by id.
func (s *Store) ListDevices(ctx context.Context) ([]model.Device, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT `+deviceCols+` FROM devices ORDER BY id`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []model.Device
	for rows.Next() {
		d, err := scanDevice(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, d)
	}
	return out, rows.Err()
}

// ---------- messages ----------

// InsertMessageTx appends one message inside tx.
func InsertMessageTx(ctx context.Context, tx *sql.Tx, m model.StoredMessage) (int64, error) {
	var seq sql.NullInt64
	if m.Seq != nil {
		seq = sql.NullInt64{Int64: *m.Seq, Valid: true}
	}
	res, err := tx.ExecContext(ctx, `
INSERT INTO messages(device_id,type,seq,sample_time,recv_at,value,is_heartbeat)
VALUES(?,?,?,?,?,?,?)`,
		m.DeviceID, m.Type, seq, m.SampleTime, ts(m.RecvAt), nf64(m.Value),
		btoi(m.IsHeartbeat))
	if err != nil {
		return 0, err
	}
	return res.LastInsertId()
}

func btoi(b bool) int {
	if b {
		return 1
	}
	return 0
}

// ListMessages returns stored messages for a device (oldest first),
// optionally limited. Used by tests and acceptance verification.
func (s *Store) ListMessages(ctx context.Context, deviceID string, limit int) ([]model.StoredMessage, error) {
	q := `SELECT device_id,type,seq,sample_time,recv_at,value,is_heartbeat
	      FROM messages WHERE device_id=? ORDER BY id`
	args := []any{deviceID}
	if limit > 0 {
		q += ` LIMIT ?`
		args = append(args, limit)
	}
	rows, err := s.db.QueryContext(ctx, q, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []model.StoredMessage
	for rows.Next() {
		var m model.StoredMessage
		var seq sql.NullInt64
		var val sql.NullFloat64
		var hb int
		var recvStr string
		if err := rows.Scan(&m.DeviceID, &m.Type, &seq, &m.SampleTime,
			&recvStr, &val, &hb); err != nil {
			return nil, err
		}
		m.RecvAt, err = time.Parse(time.RFC3339Nano, recvStr)
		if err != nil {
			return nil, err
		}
		m.Seq = pni64(seq)
		if val.Valid {
			m.Value = &val.Float64
		}
		m.IsHeartbeat = hb != 0
		out = append(out, m)
	}
	return out, rows.Err()
}

// ---------- device engine state ----------

// GetState returns the raw JSON engine state for a device.
// ok is false when no state exists yet.
func (s *Store) GetState(ctx context.Context, deviceID string) (data []byte, ok bool, err error) {
	var raw string
	err = s.db.QueryRowContext(ctx,
		`SELECT data FROM device_states WHERE device_id=?`, deviceID).Scan(&raw)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, err
	}
	return []byte(raw), true, nil
}

// PutStateTx writes engine state for a device inside tx.
func PutStateTx(ctx context.Context, tx *sql.Tx, deviceID string, data []byte, at time.Time) error {
	_, err := tx.ExecContext(ctx, `
INSERT INTO device_states(device_id,data,updated_at) VALUES(?,?,?)
ON CONFLICT(device_id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at`,
		deviceID, string(data), ts(at))
	return err
}

// AllStateDeviceIDs returns device ids that have persisted engine state.
func (s *Store) AllStateDeviceIDs(ctx context.Context) ([]string, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT device_id FROM device_states ORDER BY device_id`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var ids []string
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		ids = append(ids, id)
	}
	return ids, rows.Err()
}

// ---------- events ----------

// InsertEventTx stores a new event and populates its ID.
func InsertEventTx(ctx context.Context, tx *sql.Tx, e *model.Event) error {
	res, err := tx.ExecContext(ctx, `
INSERT INTO events(device_id,device_type,rule,open,triggered_at,config_version,
  recovered_at,recovery_config_version,start_seq,end_seq,start_sample,
  end_sample,gap_start,gap_end,note)
VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
		e.DeviceID, e.DeviceType, e.Rule, btoi(e.Open), ts(e.TriggeredAt),
		e.ConfigVersion, nts(e.RecoveredAt), ni64(recVerOrNil(e)),
		ni64(e.StartSeq), ni64(e.EndSeq), nts(e.StartSample), nts(e.EndSample),
		ni64(e.GapStart), ni64(e.GapEnd), e.Note)
	if err != nil {
		return err
	}
	id, err := res.LastInsertId()
	if err != nil {
		return err
	}
	e.ID = id
	return nil
}

func recVerOrNil(e *model.Event) *int64 {
	if e.RecoveryConfigVersion == nil {
		return nil
	}
	v := int64(*e.RecoveryConfigVersion)
	return &v
}

// GetOpenEventTx returns the currently open event for a device/rule inside tx.
func GetOpenEventTx(ctx context.Context, tx *sql.Tx, deviceID, rule string) (*model.Event, error) {
	row := tx.QueryRowContext(ctx, `
SELECT id,device_id,device_type,rule,open,triggered_at,config_version,
  recovered_at,recovery_config_version,start_seq,end_seq,start_sample,
  end_sample,gap_start,gap_end,note
FROM events WHERE device_id=? AND rule=? AND open=1`, deviceID, rule)
	e, err := scanEvent(row)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &e, nil
}

// CloseEventTx closes an open event, recording recovery metadata and the
// final trigger sample interval.
func CloseEventTx(ctx context.Context, tx *sql.Tx, e *model.Event, recoveredAt time.Time, recoveryVersion int) error {
	_, err := tx.ExecContext(ctx, `
UPDATE events SET open=0, recovered_at=?, recovery_config_version=?,
  end_seq=?, end_sample=?, note=? WHERE id=? AND open=1`,
		ts(recoveredAt), recoveryVersion, ni64(e.EndSeq), nts(e.EndSample),
		e.Note, e.ID)
	return err
}

// UpdateEventGapTx widens the missing-sequence range on an open gap event.
func UpdateEventGapTx(ctx context.Context, tx *sql.Tx, id int64, gapStart, gapEnd *int64) error {
	_, err := tx.ExecContext(ctx,
		`UPDATE events SET gap_start=?, gap_end=? WHERE id=?`,
		ni64(gapStart), ni64(gapEnd), id)
	return err
}

func scanEvent(row interface {
	Scan(dest ...any) error
}) (model.Event, error) {
	var e model.Event
	var open sql.NullInt64
	var trig string
	var rec sql.NullString
	var rver, stseq, enseq, gstart, gend sql.NullInt64
	var stsample, ensample sql.NullString
	var note sql.NullString
	if err := row.Scan(&e.ID, &e.DeviceID, &e.DeviceType, &e.Rule, &open,
		&trig, &e.ConfigVersion, &rec, &rver, &stseq, &enseq,
		&stsample, &ensample, &gstart, &gend, &note); err != nil {
		return e, err
	}
	e.Open = open.Int64 != 0
	e.TriggeredAt, _ = time.Parse(time.RFC3339Nano, trig)
	if rec.Valid {
		t, err := time.Parse(time.RFC3339Nano, rec.String)
		if err == nil {
			e.RecoveredAt = &t
		}
	}
	e.RecoveryConfigVersion = pni64ToInt(rver)
	e.StartSeq = pni64(stseq)
	e.EndSeq = pni64(enseq)
	p, _ := pts(stsample)
	e.StartSample = p
	p, _ = pts(ensample)
	e.EndSample = p
	e.GapStart = pni64(gstart)
	e.GapEnd = pni64(gend)
	if note.Valid {
		e.Note = note.String
	}
	return e, nil
}

// pni64ToInt converts a nullable int64 to a nullable *int.
func pni64ToInt(v sql.NullInt64) *int {
	if !v.Valid {
		return nil
	}
	n := int(v.Int64)
	return &n
}

// EventFilter narrows an event listing.
type EventFilter struct {
	DeviceID string
	Rule     string
	Open     *bool
	Limit    int
}

// ListEvents lists events matching the filter, newest first.
func (s *Store) ListEvents(ctx context.Context, f EventFilter) ([]model.Event, error) {
	var conds []string
	var args []any
	if f.DeviceID != "" {
		conds = append(conds, "device_id=?")
		args = append(args, f.DeviceID)
	}
	if f.Rule != "" {
		conds = append(conds, "rule=?")
		args = append(args, f.Rule)
	}
	if f.Open != nil {
		conds = append(conds, "open=?")
		args = append(args, btoi(*f.Open))
	}
	q := `SELECT id,device_id,device_type,rule,open,triggered_at,config_version,
  recovered_at,recovery_config_version,start_seq,end_seq,start_sample,
  end_sample,gap_start,gap_end,note FROM events`
	if len(conds) > 0 {
		q += ` WHERE ` + strings.Join(conds, ` AND `)
	}
	q += ` ORDER BY id DESC`
	limit := f.Limit
	if limit <= 0 || limit > 1000 {
		limit = 100
	}
	q += fmt.Sprintf(` LIMIT %d`, limit)
	rows, err := s.db.QueryContext(ctx, q, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []model.Event
	for rows.Next() {
		e, err := scanEvent(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, e)
	}
	return out, rows.Err()
}

// ---------- webhook deliveries ----------

// Delivery is one recorded webhook delivery attempt.
type Delivery struct {
	EventID    int64
	Phase      string
	URL        string
	OK         bool
	StatusCode int
	Err        string
	Attempt    int
	CreatedAt  time.Time
}

// RecordDelivery persists one delivery attempt.
func (s *Store) RecordDelivery(ctx context.Context, d Delivery) error {
	var code sql.NullInt64
	if d.StatusCode > 0 {
		code = sql.NullInt64{Int64: int64(d.StatusCode), Valid: true}
	}
	_, err := s.db.ExecContext(ctx, `
INSERT INTO webhook_deliveries(event_id,phase,url,ok,status_code,error,
  attempt,created_at) VALUES(?,?,?,?,?,?,?,?)`,
		d.EventID, d.Phase, d.URL, btoi(d.OK), code, d.Err, d.Attempt,
		ts(d.CreatedAt))
	return err
}
