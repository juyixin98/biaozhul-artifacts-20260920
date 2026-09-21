// Package timeutil handles time-of-day and local-date math for monitoring
// windows. All storage is in UTC; employee-local time is used only to apply
// the monitoring window and to pick the daily/weekly summary bucket.
package timeutil

import (
	"fmt"
	"time"
)

// LoadZone parses an IANA zone name. The model layer guarantees zone names
// contain "/", but callers should still handle the error.
func LoadZone(name string) (*time.Location, error) {
	if name == "" {
		return nil, fmt.Errorf("empty timezone")
	}
	return time.LoadLocation(name)
}

// TruncateMinute drops seconds and below, keeping the time zone.
func TruncateMinute(t time.Time) time.Time {
	return t.Truncate(time.Minute)
}

// MinuteOfDay returns 0..1439 in the given location.
func MinuteOfDay(t time.Time, loc *time.Location) int {
	local := t.In(loc)
	return local.Hour()*60 + local.Minute()
}

// WithinWindow reports whether minute-of-day m is in the half-open interval
// [start,end). end may be 1440, meaning 24:00 (end of day). An interval where
// end < start wraps past midnight; m == start is always in the window and
// m == end is always out. start == end is rejected at publish time.
func WithinWindow(m, start, end int) bool {
	if start < end {
		return m >= start && m < end
	}
	return m >= start || m < end
}

// LocalDate returns the calendar date in loc as "YYYY-MM-DD".
func LocalDate(t time.Time, loc *time.Location) string {
	local := t.In(loc)
	return local.Format("2006-01-02")
}

// WeekStart returns the Monday of the local week of t, as "YYYY-MM-DD".
func WeekStart(t time.Time, loc *time.Location) string {
	local := t.In(loc)
	days := int(local.Weekday()) - 1 // Monday=0 ... Sunday=6
	if days < 0 {
		days = 6
	}
	monday := time.Date(local.Year(), local.Month(), local.Day()-days, 0, 0, 0, 0, loc)
	return monday.Format("2006-01-02")
}

// DateInWeek reports whether "YYYY-MM-DD" date equals the Monday of weekStart
// plus offset days (0..6).
func DateInWeek(date, weekStart string) (bool, error) {
	d, err := time.Parse("2006-01-02", date)
	if err != nil {
		return false, fmt.Errorf("invalid date %q: %w", date, err)
	}
	ws, err := time.Parse("2006-01-02", weekStart)
	if err != nil {
		return false, fmt.Errorf("invalid week start %q: %w", weekStart, err)
	}
	diff := int(d.Sub(ws).Hours() / 24)
	return diff >= 0 && diff < 7, nil
}
