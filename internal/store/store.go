package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"time"

	_ "modernc.org/sqlite"

	"sensorhealth/internal/domain"
)

// Store is the SQLite-backed persistence layer. All methods are safe for
// concurrent use; serialized=true on the DSN makes SQLite itself serialise
// writers, while WAL allows readers concurrently.
type Store struct {
	db *sql.DB
}

func Open(ctx context.Context, dsn string) (*Store, error) {
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, err
	}
	// modernc.org/sqlite: one connection avoids "database is locked" under
	// WAL for this workload while still multiplexing goroutines via Go's
	// database/sql.
	db.SetMaxOpenConns(1)
	if _, err := db.ExecContext(ctx, schema); err != nil {
		db.Close()
		return nil, fmt.Errorf("apply schema: %w", err)
	}
	return &Store{db: db}, nil
}

func (s *Store) Close() error { return s.db.Close() }

// Tx runs fn inside a serialised transaction.
func (s *Store) Tx(ctx context.Context, fn func(*sql.Tx) error) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	if err := fn(tx); err != nil {
		_ = tx.Rollback()
		return err
	}
	return tx.Commit()
}

func ts(t time.Time) string { return t.UTC().Format(time.RFC3339Nano) }
func parseTS(s string) time.Time {
	t, _ := time.Parse(time.RFC3339Nano, s)
	return t
}

// ---------------------------------------------------------------------------
// Devices
// ---------------------------------------------------------------------------

func (s *Store) UpsertDevice(ctx context.Context, d domain.Device) error {
	_, err := s.db.ExecContext(ctx, `
		INSERT INTO devices(id, type, registered_at) VALUES(?,?,?)
		ON CONFLICT(id) DO UPDATE SET type=excluded.type`,
		d.ID, d.Type, ts(d.RegisteredAt))
	return err
}

func (s *Store) GetDevice(ctx context.Context, id string) (*domain.Device, error) {
	row := s.db.QueryRowContext(ctx,
		`SELECT id, type, registered_at FROM devices WHERE id=?`, id)
	var d domain.Device
	var rat string
	if err := row.Scan(&d.ID, &d.Type, &rat); err != nil {
		if err == sql.ErrNoRows {
			return nil, ErrNotFound
		}
		return nil, err
	}
	d.RegisteredAt = parseTS(rat)
	return &d, nil
}

func (s *Store) ListDevices(ctx context.Context) ([]domain.Device, error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT id, type, registered_at FROM devices ORDER BY id`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []domain.Device
	for rows.Next() {
		var d domain.Device
		var rat string
		if err := rows.Scan(&d.ID, &d.Type, &rat); err != nil {
			return nil, err
		}
		d.RegisteredAt = parseTS(rat)
		out = append(out, d)
	}
	return out, rows.Err()
}

// ---------------------------------------------------------------------------
// Rule configs
// ---------------------------------------------------------------------------

// GetRuleConfig returns the config for a device type.
func (s *Store) GetRuleConfig(ctx context.Context, deviceType string) (domain.RuleConfig, int64, error) {
	row := s.db.QueryRowContext(ctx,
		`SELECT version, payload FROM rule_configs WHERE device_type=?`, deviceType)
	var version int64
	var payload string
	if err := row.Scan(&version, &payload); err != nil {
		if err == sql.ErrNoRows {
			return domain.RuleConfig{}, 0, ErrNotFound
		}
		return domain.RuleConfig{}, 0, err
	}
	var c domain.RuleConfig
	if err := json.Unmarshal([]byte(payload), &c); err != nil {
		return domain.RuleConfig{}, 0, err
	}
	return c, version, nil
}

// PutRuleConfig inserts or bumps the version atomically.
func (s *Store) PutRuleConfig(ctx context.Context, c domain.RuleConfig) (int64, error) {
	var newVersion int64
	err := s.Tx(ctx, func(tx *sql.Tx) error {
		var cur sql.NullInt64
		if err := tx.QueryRowContext(ctx,
			`SELECT version FROM rule_configs WHERE device_type=?`, c.DeviceType).
			Scan(&cur); err != nil && err != sql.ErrNoRows {
			return err
		}
		newVersion = cur.Int64 + 1
		c.Version = newVersion
		p, err := json.Marshal(c)
		if err != nil {
			return err
		}
		_, err = tx.ExecContext(ctx, `
			INSERT INTO rule_configs(device_type, version, payload, updated_at)
			VALUES(?,?,?,?)
			ON CONFLICT(device_type) DO UPDATE SET
				version=excluded.version,
				payload=excluded.payload,
				updated_at=excluded.updated_at`,
			c.DeviceType, newVersion, string(p), ts(time.Now()))
		return err
	})
	return newVersion, err
}

// ---------------------------------------------------------------------------
// Observations
// ---------------------------------------------------------------------------

// ObservationRow is a persisted sample or heartbeat.
type ObservationRow struct {
	DeviceID   string
	Epoch      int64
	Seq        *int64
	Value      *string
	SampledAt  time.Time
	ReceivedAt time.Time
	Heartbeat  bool
}

func (s *Store) InsertObservation(ctx context.Context, o ObservationRow) error {
	_, err := s.db.ExecContext(ctx, `
		INSERT INTO observations
		(device_id, epoch, seq, value, sampled_at, received_at, is_heartbeat)
		VALUES(?,?,?,?,?,?,?)`,
		o.DeviceID, o.Epoch, o.Seq, o.Value, ts(o.SampledAt), ts(o.ReceivedAt),
		boolInt(o.Heartbeat))
	return err
}

// ExistsSample reports whether (device, epoch, seq) is already present.
func (s *Store) ExistsSample(ctx context.Context, deviceID string, epoch, seq int64) (bool, error) {
	var n int
	err := s.db.QueryRowContext(ctx,
		`SELECT COUNT(1) FROM observations
		 WHERE device_id=? AND epoch=? AND seq=? AND is_heartbeat=0`,
		deviceID, epoch, seq).Scan(&n)
	return n > 0, err
}

// ---------------------------------------------------------------------------
// Device state (per-rule JSON blobs)
// ---------------------------------------------------------------------------

// StateRow is the persisted mutable state for one device.
type StateRow struct {
	DeviceID             string
	Epoch                int64
	HighSeq              int64
	LastSampleSampledAt  *time.Time
	LastSampleReceivedAt *time.Time
	LastAnyReceivedAt    time.Time
	StaleJSON            []byte
	FrozenJSON           []byte
	GapJSON              []byte
}

func (s *Store) GetState(ctx context.Context, deviceID string) (*StateRow, error) {
	row := s.db.QueryRowContext(ctx, `
		SELECT device_id, epoch, high_seq, last_sample_sampled_at,
		       last_sample_received_at, last_any_received_at,
		       stale_state, frozen_state, gap_state
		FROM device_state WHERE device_id=?`, deviceID)
	var r StateRow
	var lss, lsr sql.NullString
	var any string
	if err := row.Scan(&r.DeviceID, &r.Epoch, &r.HighSeq, &lss, &lsr, &any,
		&r.StaleJSON, &r.FrozenJSON, &r.GapJSON); err != nil {
		if err == sql.ErrNoRows {
			return nil, ErrNotFound
		}
		return nil, err
	}
	if lss.Valid {
		t := parseTS(lss.String)
		r.LastSampleSampledAt = &t
	}
	if lsr.Valid {
		t := parseTS(lsr.String)
		r.LastSampleReceivedAt = &t
	}
	r.LastAnyReceivedAt = parseTS(any)
	return &r, nil
}

func (s *Store) PutState(ctx context.Context, r StateRow) error {
	var lss, lsr any
	if r.LastSampleSampledAt != nil {
		lss = ts(*r.LastSampleSampledAt)
	}
	if r.LastSampleReceivedAt != nil {
		lsr = ts(*r.LastSampleReceivedAt)
	}
	_, err := s.db.ExecContext(ctx, `
		INSERT INTO device_state(
			device_id, epoch, high_seq, last_sample_sampled_at,
			last_sample_received_at, last_any_received_at,
			stale_state, frozen_state, gap_state, updated_at)
		VALUES(?,?,?,?,?,?,?,?,?,?)
		ON CONFLICT(device_id) DO UPDATE SET
			epoch=excluded.epoch,
			high_seq=excluded.high_seq,
			last_sample_sampled_at=excluded.last_sample_sampled_at,
			last_sample_received_at=excluded.last_sample_received_at,
			last_any_received_at=excluded.last_any_received_at,
			stale_state=excluded.stale_state,
			frozen_state=excluded.frozen_state,
			gap_state=excluded.gap_state,
			updated_at=excluded.updated_at`,
		r.DeviceID, r.Epoch, r.HighSeq, lss, lsr, ts(r.LastAnyReceivedAt),
		string(r.StaleJSON), string(r.FrozenJSON), string(r.GapJSON),
		ts(time.Now()))
	return err
}

func (s *Store) ListStateDeviceIDs(ctx context.Context) ([]string, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT device_id FROM device_state`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		out = append(out, id)
	}
	return out, rows.Err()
}

// ---------------------------------------------------------------------------
// Alerts
// ---------------------------------------------------------------------------

func (s *Store) InsertAlert(ctx context.Context, a *domain.Alert) error {
	res, err := s.db.ExecContext(ctx, `
		INSERT INTO alerts(device_id, device_type, kind, status, config_version,
			epoch, trig_seq_start, trig_seq_end, trig_first_at, trig_last_at,
			rec_seq_start, rec_seq_end, rec_first_at, rec_last_at,
			opened_at, recovered_at, detail)
		VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
		a.DeviceID, a.DeviceType, string(a.Kind), string(a.Status), a.ConfigVersion,
		a.TriggerRange.Epoch, a.TriggerRange.SeqStart, a.TriggerRange.SeqEnd,
		ts(a.TriggerRange.FirstTime), ts(a.TriggerRange.LastTime),
		nil, nil, nil, nil,
		ts(a.OpenedAt), nil, a.Detail)
	if err != nil {
		return err
	}
	a.ID, _ = res.LastInsertId()
	return nil
}

// RecoverAlert marks the currently-open alert of (device,kind) recovered.
func (s *Store) RecoverAlert(ctx context.Context, deviceID string, kind domain.Kind,
	rng domain.SampleRange, recoveredAt time.Time) (int64, error) {
	res, err := s.db.ExecContext(ctx, `
		UPDATE alerts SET
			status=?,
			rec_seq_start=?, rec_seq_end=?,
			rec_first_at=?, rec_last_at=?,
			recovered_at=?
		WHERE device_id=? AND kind=? AND status=?`,
		string(domain.StatusRecovered),
		rng.SeqStart, rng.SeqEnd, ts(rng.FirstTime), ts(rng.LastTime),
		ts(recoveredAt),
		deviceID, string(kind), string(domain.StatusOpen))
	if err != nil {
		return 0, err
	}
	n, _ := res.RowsAffected()
	return n, nil
}

// ListAlerts filters by optional device/status/kind.
func (s *Store) ListAlerts(ctx context.Context, deviceID string, status domain.AlertStatus, kind domain.Kind, limit int) ([]domain.Alert, error) {
	q := ` WHERE 1=1`
	args := []any{}
	if deviceID != "" {
		q += ` AND device_id=?`
		args = append(args, deviceID)
	}
	if status != "" {
		q += ` AND status=?`
		args = append(args, string(status))
	}
	if kind != "" {
		q += ` AND kind=?`
		args = append(args, string(kind))
	}
	q += ` ORDER BY id DESC`
	if limit > 0 {
		q += ` LIMIT ?`
		args = append(args, limit)
	}
	return s.queryAlerts(ctx, q, args...)
}

const alertCols = `id, device_id, device_type, kind, status, config_version,
	epoch, trig_seq_start, trig_seq_end, trig_first_at, trig_last_at,
	rec_seq_start, rec_seq_end, rec_first_at, rec_last_at,
	opened_at, recovered_at, COALESCE(detail,'')`

func (s *Store) queryAlerts(ctx context.Context, where string, args ...any) ([]domain.Alert, error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT `+alertCols+` FROM alerts `+where, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []domain.Alert
	for rows.Next() {
		a, err := scanAlert(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, a)
	}
	return out, rows.Err()
}

func scanAlert(rows *sql.Rows) (domain.Alert, error) {
	var a domain.Alert
	var kind, status string
	var trigFirst, trigLast string
	var recS, recE sql.NullInt64
	var recFirst, recLast, recovered sql.NullString
	var opened string
	var id int64
	var cfgVer, epoch, tss, tse int64
	var dev, devType string
	var detail string
	if err := rows.Scan(&id, &dev, &devType, &kind, &status, &cfgVer,
		&epoch, &tss, &tse, &trigFirst, &trigLast,
		&recS, &recE, &recFirst, &recLast, &opened, &recovered, &detail); err != nil {
		return a, err
	}
	a.ID, a.DeviceID, a.DeviceType = id, dev, devType
	a.Kind, a.Status = domain.Kind(kind), domain.AlertStatus(status)
	a.ConfigVersion = cfgVer
	a.TriggerRange = domain.SampleRange{
		Epoch: epoch, SeqStart: tss, SeqEnd: tse,
		FirstTime: parseTS(trigFirst), LastTime: parseTS(trigLast),
	}
	if recS.Valid {
		r := domain.SampleRange{
			Epoch: epoch, SeqStart: recS.Int64, SeqEnd: recE.Int64,
			FirstTime: parseTS(recFirst.String),
			LastTime:  parseTS(recLast.String),
		}
		a.RecoverRange = &r
	}
	a.OpenedAt = parseTS(opened)
	if recovered.Valid {
		t := parseTS(recovered.String)
		a.RecoveredAt = &t
	}
	a.Detail = detail
	return a, nil
}

func boolInt(b bool) int {
	if b {
		return 1
	}
	return 0
}
