// Package domain defines the core domain model for sensor health determination.
//
// Three distinct timestamps are modeled on every observation:
//
//   - SampleTime  (sampled_at): the instant the device sampled the value on the
//     device clock.
//   - ReceiveTime (received_at): the instant the ingestion HTTP server accepted
//     the message, on the server clock.
//   - Heartbeat   (is_heartbeat): a message that proves the device is alive but
//     carries no new sample value/sequence.
//
// Health rules:
//
//   - stale  — no message (sample OR heartbeat) was received within
//     StaleEnterTimeout; recovered after messages arrive continuously for
//     StaleRecoverTimeout (< enter timeout).
//   - frozen — value constant while sequence numbers keep advancing. Triggered
//     only after FrozenEnterCount consecutive equal-valued samples spanning at
//     least FrozenEnterMinDuration of device SampleTime; recovered after
//     FrozenRecoverCount consecutive different-valued samples. Thresholds are
//     per device type so genuinely stationary devices are not flagged.
//   - gap    — a sequence number larger than expected arrived, leaving a hole.
//     Triggered once MissingEnterCount (or more) consecutive sequences are
//     missing; recovered when the hole shrinks below MissingRecoverCount,
//     including via out-of-order batch backfill.
//
// A backwards jump of the device clock (SampleTime relative to the last sample
// on the same sequence line), or an explicit sequence reset, opens a new
// "epoch": gap/frozen state never spans an epoch boundary.
package domain

import "time"

// Kind enumerates the health rule kinds.
type Kind string

const (
	KindStale  Kind = "stale"
	KindFrozen Kind = "frozen"
	KindGap    Kind = "gap"
)

// AlertStatus is the lifecycle status of an alert for one (device, kind).
type AlertStatus string

const (
	StatusOpen      AlertStatus = "open"
	StatusRecovered AlertStatus = "recovered"
)

// RuleConfig is the per-device-type health configuration. Versioned: every
// update produces a new monotonically increasing version; alerts and health
// snapshots record the version that was active.
type RuleConfig struct {
	DeviceType string `json:"device_type"`
	Version    int64  `json:"version"`

	// stale rule (based on server ReceiveTime; heartbeats count)
	StaleEnterTimeout   Duration `json:"stale_enter_timeout"`
	StaleRecoverTimeout Duration `json:"stale_recover_timeout"`

	// frozen rule (fixed value; based on samples, not heartbeats)
	FrozenEnterCount       int      `json:"frozen_enter_count"`
	FrozenEnterMinDuration Duration `json:"frozen_enter_min_duration"`
	FrozenRecoverCount     int      `json:"frozen_recover_count"`

	// gap rule (sequence holes; backfill can recover)
	MissingEnterCount   int `json:"missing_enter_count"`
	MissingRecoverCount int `json:"missing_recover_count"`

	// Out-of-order tolerance: samples arriving with seq <= high watermark
	// but within this many sequences below it are treated as backfill of the
	// current epoch rather than evidence of a clock rollback.
	BackfillLookback int64 `json:"backfill_lookback"`

	// Max timestamp-skew claimed by senders is enforced at the transport layer
	// (crypto.Signer), not here.
}

// Device is a registered sensor.
type Device struct {
	ID           string    `json:"id"`
	Type         string    `json:"type"`
	RegisteredAt time.Time `json:"registered_at"`
}

// SampleRange identifies the inclusive [SeqStart, SeqEnd] span of samples
// within one epoch that caused an alert to open (and, on recovery, the span
// that healed it).
type SampleRange struct {
	Epoch     int64     `json:"epoch"`
	SeqStart  int64     `json:"seq_start"`
	SeqEnd    int64     `json:"seq_end"`
	FirstTime time.Time `json:"first_sample_time"`
	LastTime  time.Time `json:"last_sample_time"`
}

// Alert is one open/recovered incident for a (device, kind).
type Alert struct {
	ID int64 `json:"id"`

	DeviceID   string      `json:"device_id"`
	DeviceType string      `json:"device_type"`
	Kind       Kind        `json:"kind"`
	Status     AlertStatus `json:"status"`

	// ConfigVersion is the rule-config version frozen at trigger time.
	ConfigVersion int64 `json:"config_version"`

	// TriggerRange is the sample interval that opened the alert.
	TriggerRange SampleRange `json:"trigger_range"`
	// RecoverRange is the sample interval that satisfied the recovery
	// threshold (nil while open).
	RecoverRange *SampleRange `json:"recover_range,omitempty"`

	OpenedAt    time.Time  `json:"opened_at"`
	RecoveredAt *time.Time `json:"recovered_at,omitempty"`

	// Kind-specific human-readable detail.
	Detail string `json:"detail"`
}

// Health is the current per-device snapshot returned by the API.
type Health struct {
	DeviceID      string               `json:"device_id"`
	DeviceType    string               `json:"device_type"`
	Healthy       bool                 `json:"healthy"`
	OpenAlerts    []Kind               `json:"open_alerts"`
	ConfigVersion int64                `json:"config_version"`
	Epoch         int64                `json:"epoch"`
	LastSeq       int64                `json:"last_seq"`
	LastSampleAt  *time.Time           `json:"last_sample_at,omitempty"`
	LastReceiveAt time.Time            `json:"last_receive_at"`
	Details       map[Kind]AlertDetail `json:"details"`
}

// AlertDetail carries rule-specific snapshot data.
type AlertDetail struct {
	Status AlertStatus    `json:"status"`
	Range  SampleRange    `json:"range"`
	Since  time.Time      `json:"since"`
	Extra  map[string]any `json:"extra,omitempty"`
}

// IngestResult is what the service returns after accepting a batch.
type IngestResult struct {
	Accepted     int      `json:"accepted"`
	Rejected     int      `json:"rejected"`
	RejectReason []string `json:"reject_reasons,omitempty"`
	DeviceID     string   `json:"device_id"`
	Epoch        int64    `json:"epoch"`
	HighSeq      int64    `json:"high_seq"`
}
