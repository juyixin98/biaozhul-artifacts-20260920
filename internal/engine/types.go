// Package engine implements the alert hysteresis state machine driven by a
// virtual clock. It is intentionally independent of HTTP and persistence so
// the state logic can be unit-tested in isolation.
package engine

import (
	"encoding/json"
	"errors"
	"fmt"
	"time"
)

// State of a rule's evaluation lifecycle.
type State string

const (
	// StateInactive: latest fresh sample is not hot (below/above threshold
	// depending on operator), no pending condition.
	StateInactive State = "inactive"
	// StatePending: hot samples seen, but the hot streak is shorter than
	// TriggerFor. No notification is emitted in this state.
	StatePending State = "pending"
	// StateFiring: hot streak has lasted at least TriggerFor. A "firing"
	// notification is emitted on entry.
	StateFiring State = "firing"
	// StateRecovering: cold samples seen while firing, but the cold streak
	// is shorter than RecoverFor. Hysteresis keeps the alert open.
	StateRecovering State = "recovering"
	// StateNodata: time has advanced NoDataFor past the last received sample.
	// A "nodata" notification is emitted on entry.
	StateNodata State = "nodata"
)

// EventType labels an event. Only transitions into firing, resolved, nodata
// and data_resumed are user-facing notifications; rule_reset is an audit
// event emitted when a rule's configuration is replaced.
type EventType string

const (
	EventFiring      EventType = "firing"
	EventResolved    EventType = "resolved"
	EventNodata      EventType = "nodata"
	EventDataResumed EventType = "data_resumed"
	EventRuleReset   EventType = "rule_reset"
)

// IsNotification reports whether the event is a user-facing notification
// (the /events API defaults to returning only these).
func (t EventType) IsNotification() bool {
	switch t {
	case EventFiring, EventResolved, EventNodata, EventDataResumed:
		return true
	default:
		return false
	}
}

func validState(s State) bool {
	switch s {
	case StateInactive, StatePending, StateFiring, StateRecovering, StateNodata:
		return true
	default:
		return false
	}
}

// Operator compares a sample value against the rule threshold.
type Operator string

const (
	OpGreaterThan    Operator = ">"
	OpGreaterOrEqual Operator = ">="
	OpLessThan       Operator = "<"
	OpLessOrEqual    Operator = "<="
)

func validOp(op Operator) bool {
	switch op {
	case OpGreaterThan, OpGreaterOrEqual, OpLessThan, OpLessOrEqual:
		return true
	default:
		return false
	}
}

// upward reports whether the hot condition is "value is high" (>=, >).
func (op Operator) upward() bool {
	return op == OpGreaterThan || op == OpGreaterOrEqual
}

// hot reports whether value breaches the threshold for this operator.
func (op Operator) hot(value, threshold float64) bool {
	switch op {
	case OpGreaterThan:
		return value > threshold
	case OpGreaterOrEqual:
		return value >= threshold
	case OpLessThan:
		return value < threshold
	case OpLessOrEqual:
		return value <= threshold
	default:
		return false
	}
}

// Duration is a time.Duration with JSON support for both the native int64
// nanosecond form and human strings ("60s", "1m"). Samples use seconds in
// the API examples, and "60s" is the ergonomic form.
type Duration struct {
	time.Duration
}

func (d Duration) MarshalJSON() ([]byte, error) {
	return json.Marshal(d.Duration.String())
}

func (d *Duration) UnmarshalJSON(b []byte) error {
	var s string
	if err := json.Unmarshal(b, &s); err == nil {
		parsed, perr := time.ParseDuration(s)
		if perr != nil {
			return fmt.Errorf("invalid duration %q: %w", s, perr)
		}
		d.Duration = parsed
		return nil
	}
	// Fall back to a numeric value interpreted as seconds (common for
	// synthetic monitoring payloads).
	var secs float64
	if err := json.Unmarshal(b, &secs); err != nil {
		return errors.New("duration must be a string like \"60s\" or a number of seconds")
	}
	if secs < 0 {
		return errors.New("duration must not be negative")
	}
	d.Duration = time.Duration(secs * float64(time.Second))
	return nil
}

// VTime is a time.Time that accepts either an RFC3339 string or a number
// (Unix seconds) in JSON. All engine time comes from the virtual clock, so
// timestamps in payloads are virtual-time coordinates.
type VTime struct {
	time.Time
}

func (t VTime) MarshalJSON() ([]byte, error) {
	return json.Marshal(t.Time.UTC().Format(time.RFC3339Nano))
}

func (t *VTime) UnmarshalJSON(b []byte) error {
	var s string
	if err := json.Unmarshal(b, &s); err == nil {
		parsed, perr := time.Parse(time.RFC3339Nano, s)
		if perr != nil {
			return fmt.Errorf("invalid timestamp %q: %w", s, perr)
		}
		t.Time = parsed.UTC()
		return nil
	}
	var num float64
	if err := json.Unmarshal(b, &num); err != nil {
		return errors.New(`timestamp must be an RFC3339 string or Unix seconds number`)
	}
	secs := int64(num)
	t.Time = time.Unix(secs, int64((num-float64(secs))*float64(time.Second))).UTC()
	return nil
}
