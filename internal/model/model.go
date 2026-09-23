// Package model holds the domain types shared by store, engine and httpapi.
package model

import (
	"errors"
	"strings"
	"time"
)

// Rule identifiers.
const (
	RuleStale = "stale"
	RuleFixed = "fixed_value"
	RuleGap   = "sequence_gap"
)

// Rule states.
const (
	StateOK     = "OK"
	StateAlert  = "ALERT"
	EventOpen   = true
	EventClosed = false
)

// DeviceType is the per-type health-detection configuration.
// Thresholds differ for entering and leaving an alert on purpose (hysteresis).
type DeviceType struct {
	// Type is the device type name, e.g. "temperature".
	Type string `json:"type"`
	// StaleEnterSec: alert when no message (data or heartbeat) has been
	// received for longer than this.
	StaleEnterSec int `json:"stale_enter_sec"`
	// StaleRecoverSec: clear a stale alert only when the most recent
	// message was received within this (must be < StaleEnterSec).
	StaleRecoverSec int `json:"stale_recover_sec"`
	// FixedWindowCount: number of consecutive equal samples that enters a
	// fixed_value alert. 0 disables fixed-value detection for this type,
	// so devices that legitimately report a constant value (switches,
	// door contacts) are never judged broken for being stationary.
	FixedWindowCount int `json:"fixed_window_count"`
	// FixedTolerance: absolute differences <= this count as "equal".
	FixedTolerance float64 `json:"fixed_tolerance"`
	// GapRecoverGapless: number of consecutive gap-free forward samples
	// required to clear a sequence_gap alert.
	GapRecoverGapless int `json:"gap_recover_gapless"`
	// ClockSkewTolMs: backwards device-clock movement within this
	// tolerance is treated as jitter, not a clock rollback.
	ClockSkewTolMs int `json:"clock_skew_tol_ms"`
	// Version is the configuration version in force when this row was last
	// written; it is attached to every alert event.
	Version int `json:"version"`
	// UpdatedAt is the server time of the last configuration write.
	UpdatedAt time.Time `json:"updated_at"`
}

// Validate checks the thresholds for internal consistency.
func (d DeviceType) Validate() error {
	if strings.TrimSpace(d.Type) == "" {
		return errors.New("type is required")
	}
	if d.StaleEnterSec <= 0 {
		return errors.New("stale_enter_sec must be > 0")
	}
	if d.StaleRecoverSec <= 0 {
		return errors.New("stale_recover_sec must be > 0")
	}
	if d.StaleRecoverSec >= d.StaleEnterSec {
		return errors.New("stale_recover_sec must be < stale_enter_sec (hysteresis)")
	}
	if d.FixedWindowCount < 0 {
		return errors.New("fixed_window_count must be >= 0")
	}
	if d.FixedTolerance < 0 {
		return errors.New("fixed_tolerance must be >= 0")
	}
	if d.GapRecoverGapless < 1 {
		return errors.New("gap_recover_gapless must be >= 1")
	}
	if d.ClockSkewTolMs < 0 {
		return errors.New("clock_skew_tol_ms must be >= 0")
	}
	return nil
}

// Message is one synthetic sensor message as sent on the wire.
// Device time (SampleTime) and server receive time are deliberately separate:
// SampleTime is the device's own sampling clock, the server stamps RecvAt.
type Message struct {
	// DeviceID identifies the device.
	DeviceID string `json:"device_id"`
	// Type names the device type configuration to apply.
	Type string `json:"type"`
	// Seq is the per-device, per-clock-epoch monotonic sequence number.
	// Absent for heartbeats and allowed to be absent on data messages.
	Seq *int64 `json:"seq,omitempty"`
	// SampleTime is device sampling time, RFC3339.
	SampleTime string `json:"sample_time"`
	// Value is the measurement; absent for heartbeats.
	Value *float64 `json:"value,omitempty"`
	// IsHeartbeat marks a pure liveness message (no value semantics).
	IsHeartbeat bool `json:"is_heartbeat"`
}

// StoredMessage is a Message annotated with the server receive timestamp.
type StoredMessage struct {
	Message
	RecvAt time.Time `json:"recv_at"`
}

// Device is the persisted per-device projection.
type Device struct {
	ID            string
	Type          string
	Epoch         int
	HasSeq        bool
	LastSeq       int64
	LastSample    *time.Time
	LastHeartbeat *time.Time
	LastRecvAt    time.Time
	CreatedAt     time.Time
}

// Event is one alert lifecycle row: ENTER when Open is true, RECOVER when the
// same row is later closed (RecoveredAt set). The trigger sample interval is
// carried by StartSeq/EndSeq and StartSample/EndSample; sequence_gap alerts
// additionally name the missing sequence range.
type Event struct {
	ID         int64  `json:"id"`
	DeviceID   string `json:"device_id"`
	DeviceType string `json:"device_type"`
	Rule       string `json:"rule"`
	Open       bool   `json:"open"`
	// TriggeredAt is the server receive time at which the alert fired.
	TriggeredAt time.Time `json:"triggered_at"`
	// ConfigVersion is the configuration version that produced the alert.
	ConfigVersion int        `json:"config_version"`
	RecoveredAt   *time.Time `json:"recovered_at,omitempty"`
	// RecoveryConfigVersion is the config version in force at recovery.
	RecoveryConfigVersion *int `json:"recovery_config_version,omitempty"`

	StartSeq    *int64     `json:"start_seq,omitempty"`
	EndSeq      *int64     `json:"end_seq,omitempty"`
	StartSample *time.Time `json:"start_sample,omitempty"`
	EndSample   *time.Time `json:"end_sample,omitempty"`

	GapStart *int64 `json:"gap_start,omitempty"`
	GapEnd   *int64 `json:"gap_end,omitempty"`

	Note string `json:"note,omitempty"`
}

// RuleHealth is one rule's current state for a device.
type RuleHealth struct {
	State   string    `json:"state"`
	Since   time.Time `json:"since"`
	EventID int64     `json:"event_id,omitempty"`
	Detail  string    `json:"detail,omitempty"`
}

// Health is the full health snapshot of one device.
type Health struct {
	DeviceID       string                `json:"device_id"`
	DeviceType     string                `json:"device_type"`
	Epoch          int                   `json:"epoch"`
	LastSeq        *int64                `json:"last_seq,omitempty"`
	LastSampleTime *time.Time            `json:"last_sample_time,omitempty"`
	LastHeartbeat  *time.Time            `json:"last_heartbeat,omitempty"`
	LastRecvAt     *time.Time            `json:"last_recv_at,omitempty"`
	ServerTime     time.Time             `json:"server_time"`
	ConfigVersion  int                   `json:"config_version"`
	Rules          map[string]RuleHealth `json:"rules"`
}
