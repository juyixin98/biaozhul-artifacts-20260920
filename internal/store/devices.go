package store

import (
	"context"
	"fmt"
)

// Device is a provisioned device record.
type Device struct {
	ID     string `json:"device_id"`
	Secret string `json:"-"` // HMAC secret; never serialized to clients
}

// SeedDevice registers a device with its HMAC secret (idempotent).
func SeedDevice(ctx context.Context, id, secret string) error {
	_, err := DB().Exec(ctx, `
		INSERT INTO devices (device_id, hmac_secret)
		VALUES ($1,$2)
		ON CONFLICT (device_id) DO UPDATE SET hmac_secret = EXCLUDED.hmac_secret
	`, id, secret)
	if err != nil {
		return fmt.Errorf("seed device: %w", err)
	}
	return nil
}

// DeviceSecret returns the provisioned HMAC secret.
func DeviceSecret(ctx context.Context, id string) (string, bool, error) {
	var secret string
	err := DB().QueryRow(ctx,
		`SELECT hmac_secret FROM devices WHERE device_id=$1`, id).Scan(&secret)
	if err != nil {
		return "", false, nil // unknown device → caller quarantines
	}
	return secret, true, nil
}

// Snapshot is the acceptance-test read model.
type Snapshot struct {
	DeviceID        string  `json:"device_id"`
	CurrentBootGen  int64   `json:"current_boot_gen"`
	CurrentSeq      int64   `json:"current_seq"`
	LastValue       float64 `json:"last_value"`
	LastEventAgoSec float64 `json:"last_event_ago_sec"`
	Online          bool    `json:"online"`
}

// SnapshotSince lists current device states, marking online if the last real
// event is within the given window. Retained snapshots never influence this.
func SnapshotSince(ctx context.Context, onlineWindowSec float64) ([]Snapshot, error) {
	rows, err := DB().Query(ctx, `
		SELECT device_id, current_boot_gen, current_seq, last_value,
		       EXTRACT(EPOCH FROM (now() - last_event_at)),
		       EXTRACT(EPOCH FROM (now() - last_event_at)) <= $1
		FROM device_state
		ORDER BY device_id
	`, onlineWindowSec)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Snapshot
	for rows.Next() {
		var s Snapshot
		if err := rows.Scan(&s.DeviceID, &s.CurrentBootGen, &s.CurrentSeq,
			&s.LastValue, &s.LastEventAgoSec, &s.Online); err != nil {
			return nil, err
		}
		out = append(out, s)
	}
	return out, rows.Err()
}

// Count is a generic integer result for verification queries.
func Count(ctx context.Context, where string, args ...any) (int64, error) {
	var n int64
	err := DB().QueryRow(ctx, "SELECT count(*) FROM "+where, args...).Scan(&n)
	return n, err
}

// MaxEventSeq returns the highest committed seq for a (device, boot), or 0.
func MaxEventSeq(ctx context.Context, deviceID string, bootGen int64) (int64, error) {
	var n int64
	err := DB().QueryRow(ctx, `
		SELECT COALESCE(MAX(seq),0) FROM events
		WHERE device_id=$1 AND boot_gen=$2
	`, deviceID, bootGen).Scan(&n)
	return n, err
}
