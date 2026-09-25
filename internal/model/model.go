// Package model defines the core domain types of the alert hysteresis state
// machine: rules, per-rule runtime state, ingested samples and notification
// events. All time values are int64 Unix milliseconds unless noted otherwise.
package model

import (
	"encoding/json"
	"errors"
	"time"
)

// Status values of a rule's evaluation state.
const (
	StatusOK         = "ok"         // healthy, last seen value on the good side
	StatusPending    = "pending"    // threshold breached, but pending_for not yet sustained
	StatusAlerting   = "alerting"   // firing: threshold breach sustained for pending_for
	StatusRecovering = "recovering" // was alerting, value back on the good side but not yet sustained
	StatusNoData     = "no_data"    // no sample received for longer than no_data_for
)

// Threshold comparison directions.
const (
	DirectionAbove = "above" // breach when value > threshold
	DirectionBelow = "below" // breach when value < threshold
)

// Event types. An event is emitted ONLY on a state transition.
const (
	EventFiring      = "firing"        // pending    -> alerting
	EventResolved    = "resolved"      // recovering -> ok
	EventNoDataStart = "no_data_start" // any data-bearing state -> no_data
	EventNoDataEnd   = "no_data_end"   // no_data    -> ok / pending
	EventReset       = "reset"         // configuration change wiped the state
)

// DurationMS is a duration serialized as milliseconds in JSON, but also
// accepts human-readable strings such as "30s" or "2m" on input.
type DurationMS int64

func (d DurationMS) Milliseconds() int64 { return int64(d) }

// UnmarshalJSON accepts a number (milliseconds) or a string like "30s", "2m".
func (d *DurationMS) UnmarshalJSON(b []byte) error {
	var n int64
	if err := json.Unmarshal(b, &n); err == nil {
		if n < 0 {
			return errors.New("duration must be >= 0 milliseconds")
		}
		*d = DurationMS(n)
		return nil
	}
	var s string
	if err := json.Unmarshal(b, &s); err != nil {
		return errors.New("duration must be milliseconds (number) or a string like \"30s\"")
	}
	if s == "" {
		*d = 0
		return nil
	}
	parsed, err := time.ParseDuration(s)
	if err != nil {
		return err
	}
	if parsed < 0 {
		return errors.New("duration must be >= 0")
	}
	*d = DurationMS(parsed / time.Millisecond)
	return nil
}

func (d DurationMS) MarshalJSON() ([]byte, error) {
	return json.Marshal(int64(d))
}

// Rule is the alert configuration for one metric.
type Rule struct {
	ID          string     `json:"id"`
	Metric      string     `json:"metric"`
	Threshold   float64    `json:"threshold"`
	Direction   string     `json:"direction"`
	PendingFor  DurationMS `json:"pending_for"`  // sustained breach required to fire
	RecoveryFor DurationMS `json:"recovery_for"` // sustained recovery required to resolve
	NoDataFor   DurationMS `json:"no_data_for"`  // 0 disables the no-data check
	Enabled     bool       `json:"enabled"`
	CreatedAtMS int64      `json:"created_at_ms"`
	UpdatedAtMS int64      `json:"updated_at_ms"`
}

// State is the runtime state of one rule.
type State struct {
	RuleID string `json:"rule_id"`
	Metric string `json:"metric"`
	Status string `json:"status"`
	// EnteredAtMS is the start time of the current status's qualifying run:
	// pending: first breach sample; alerting: firing deadline / latest breach
	// sample; recovering: first good sample after alerting.
	EnteredAtMS int64 `json:"entered_at_ms"`
	// LastSeenMS is the timestamp of the last applied (non-duplicate,
	// non-late) sample.
	LastSeenMS  int64   `json:"last_seen_ms"`
	LastValue   float64 `json:"last_value"`
	WatermarkMS int64   `json:"watermark_ms"` // all data up to this time has been evaluated
}

// Sample is one ingested metric observation.
type Sample struct {
	Metric string  `json:"metric"`
	TSMS   int64   `json:"ts_ms"`
	Value  float64 `json:"value"`
}

// Event is a notification emitted on a state transition.
type Event struct {
	ID      int64  `json:"id"`
	TSMS    int64  `json:"ts_ms"`
	RuleID  string `json:"rule_id"`
	Metric  string `json:"metric"`
	Type    string `json:"type"`
	From    string `json:"from"`
	To      string `json:"to"`
	Message string `json:"message"`
}
