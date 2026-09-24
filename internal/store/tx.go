package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"time"

	"scaler-stable-window/internal/hpa"
)

// ErrDecisionExists signals that the same event time was already decided.
// The service translates it into an idempotent replay of the stored result.
var ErrDecisionExists = errors.New("decision for this metric timestamp already exists")

// tx adapts a database transaction to hpa.Tx.
type tx struct {
	ctx      context.Context
	tx       *sql.Tx
	scalerID string
}

func (t *tx) LastMetricTS() (time.Time, bool, error) {
	var ns int64
	err := t.tx.QueryRowContext(t.ctx,
		`SELECT last_metric_ns FROM scaler_meta WHERE scaler_id=?`,
		t.scalerID).Scan(&ns)
	if errors.Is(err, sql.ErrNoRows) {
		return time.Time{}, false, nil
	}
	if err != nil {
		return time.Time{}, false, err
	}
	return time.Unix(0, ns).UTC(), true, nil
}

func (t *tx) SetLastMetricTS(ts time.Time) error {
	// Idempotency: if a decision already exists for exactly this event time,
	// abort the new computation so the caller can replay the stored answer.
	var exists int
	if err := t.tx.QueryRowContext(t.ctx,
		`SELECT COUNT(*) FROM decisions WHERE scaler_id=? AND metric_ts_ns=?`,
		t.scalerID, ts.UnixNano()).Scan(&exists); err != nil {
		return err
	}
	if exists > 0 {
		return ErrDecisionExists
	}
	_, err := t.tx.ExecContext(t.ctx,
		`INSERT INTO scaler_meta(scaler_id, last_metric_ns) VALUES(?,?)
		 ON CONFLICT(scaler_id) DO UPDATE SET last_metric_ns=excluded.last_metric_ns`,
		t.scalerID, ts.UnixNano())
	return err
}

func (t *tx) InsertRecommendation(version int64, ts time.Time, rawDesired int) error {
	_, err := t.tx.ExecContext(t.ctx,
		`INSERT INTO recommendations
		 (scaler_id, config_version, metric_ts_ns, raw_desired, created_at_ns)
		 VALUES(?,?,?,?,?)`,
		t.scalerID, version, ts.UnixNano(), rawDesired, time.Now().UnixNano())
	return err
}

func (t *tx) WindowMax(version int64, from, to time.Time) (int, int, error) {
	var maxDesired, samples sql.NullInt64
	err := t.tx.QueryRowContext(t.ctx,
		`SELECT MAX(raw_desired), COUNT(*) FROM recommendations
		 WHERE scaler_id=? AND config_version=? AND metric_ts_ns BETWEEN ? AND ?`,
		t.scalerID, version, from.UnixNano(), to.UnixNano()).
		Scan(&maxDesired, &samples)
	if err != nil {
		return 0, 0, err
	}
	n := int(samples.Int64)
	if n == 0 {
		return 0, 0, nil
	}
	return int(maxDesired.Int64), n, nil
}

func (t *tx) InsertDecision(d *hpa.Decision) error {
	body, err := json.Marshal(d)
	if err != nil {
		return err
	}
	_, err = t.tx.ExecContext(t.ctx,
		`INSERT INTO decisions
		 (scaler_id, config_version, metric_ts_ns, decided_at_ns,
		  final_desired, final_action, body)
		 VALUES(?,?,?,?,?,?,?)`,
		d.ScalerID, d.ConfigVersion, d.MetricTimestamp.UnixNano(),
		d.DecidedAt.UnixNano(), d.FinalDesired, string(d.FinalAction), string(body))
	return err
}

// Compile-time assertion that tx satisfies the engine interface.
var _ hpa.Tx = (*tx)(nil)
