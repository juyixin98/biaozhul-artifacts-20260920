// Package rules defines rule parameter shapes and time-window math.
//
// Window semantics (documented and enforced):
//
// Frequency — a sliding 5-minute check over a 1-minute STEP grid. Window
// starts are truncated to the minute in the organization timezone; for an
// event at t the five stepped windows containing t are
// [t0-k*step, t0-k*step+window) for k=0..4, i.e. boundaries are HALF OPEN:
// an event at exactly window_end is NOT counted (it belongs to the next
// window). A window with more than `threshold` events raises one alert,
// bound to the rule version effective at the window start.
//
// Sensitive hours — the allowed local-time interval is [start_hour, end_hour)
// on the organization timezone clock; e.g. [06:00,20:00) means access at
// 20:00:00 sharp is already outside (alert), 06:00:00 sharp is allowed.
package rules

import (
	"encoding/json"
	"fmt"
	"time"
)

const Step = time.Minute

type FrequencyParams struct {
	WindowSeconds int      `json:"window_seconds"`
	Threshold     int      `json:"threshold"`
	Actions       []string `json:"actions"` // empty/nil = every action category
}

type SensitiveTable struct {
	Schema string `json:"schema"`
	Table  string `json:"table"`
}

type SensitiveHoursParams struct {
	SensitiveTables  []SensitiveTable `json:"sensitive_tables"`
	AllowedStartHour int              `json:"allowed_start_hour"`
	AllowedEndHour   int              `json:"allowed_end_hour"`
	Actions          []string         `json:"actions"` // categories that count as "access"; default select
}

func ParseFrequency(raw []byte) (FrequencyParams, error) {
	var p FrequencyParams
	if err := json.Unmarshal(raw, &p); err != nil {
		return p, fmt.Errorf("frequency params: %w", err)
	}
	if p.WindowSeconds <= 0 {
		p.WindowSeconds = 300
	}
	if p.Threshold <= 0 {
		p.Threshold = 500
	}
	return p, nil
}

func ParseSensitiveHours(raw []byte) (SensitiveHoursParams, error) {
	var p SensitiveHoursParams
	if err := json.Unmarshal(raw, &p); err != nil {
		return p, fmt.Errorf("sensitive_hours params: %w", err)
	}
	if p.AllowedStartHour == 0 && p.AllowedEndHour == 0 {
		p.AllowedStartHour, p.AllowedEndHour = 6, 20
	}
	if len(p.Actions) == 0 {
		p.Actions = []string{"select"}
	}
	return p, nil
}

func (p SensitiveHoursParams) MatchesTable(schema, table string) bool {
	for _, t := range p.SensitiveTables {
		if t.Schema == schema && t.Table == table {
			return true
		}
	}
	return false
}

func (p SensitiveHoursParams) MatchesAction(action string) bool {
	if len(p.Actions) == 0 {
		return true
	}
	for _, a := range p.Actions {
		if a == action {
			return true
		}
	}
	return false
}

// Allowed reports whether a wall-clock time is inside the allowed half-open
// interval [startHour, endHour). End < start encodes an overnight window
// (e.g. 22..06); End == Start means the whole day is disallowed.
func (p SensitiveHoursParams) Allowed(local time.Time) bool {
	h := local.Hour()
	s, e := p.AllowedStartHour, p.AllowedEndHour
	if s == e {
		return false
	}
	if s < e {
		return h >= s && h < e
	}
	// overnight: allowed from s..24 and 0..e
	return h >= s || h < e
}

// WindowStart truncates t to the 1-minute step grid in the given location.
// Stepping in local time keeps windows aligned to the org wall clock even
// across DST transitions.
func WindowStart(t time.Time, loc *time.Location) time.Time {
	lt := t.In(loc)
	return time.Date(lt.Year(), lt.Month(), lt.Day(), lt.Hour(), lt.Minute(), 0, 0, loc)
}

// ContainingWindows returns the distinct stepped window starts whose half-open
// [start, start+window) intervals contain t. They are returned newest-first.
func ContainingWindows(t time.Time, window time.Duration, loc *time.Location) []time.Time {
	t0 := WindowStart(t, loc)
	n := int(window / Step)
	if n < 1 {
		n = 1
	}
	out := make([]time.Time, 0, n)
	for k := 0; k < n; k++ {
		out = append(out, t0.Add(-time.Duration(k)*Step))
	}
	return out
}
