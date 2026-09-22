// Package timeutil holds timezone-aware time bucketing used by the detection
// engine. Night/stat windows are based on the employee's local wall-clock day;
// burst windows are fixed UTC tumbling windows.
package timeutil

import (
	"time"
)

// LoadLocation is time.LoadLocation with a UTC fallback for unknown zones.
func LoadLocation(name string) *time.Location {
	if name == "" {
		return time.UTC
	}
	loc, err := time.LoadLocation(name)
	if err != nil {
		return time.UTC
	}
	return loc
}

// LocalDate returns the calendar date in loc for the UTC instant t.
func LocalDate(t time.Time, loc *time.Location) time.Time {
	y, m, d := t.In(loc).Date()
	return time.Date(y, m, d, 0, 0, 0, 0, loc)
}

// DateLabel returns the local calendar date of t as a date-only value at UTC
// midnight. Window keys/dedup use this stable label, while WindowStart/End
// carry the true instants. (No UTC offset reaches 24h, so converting the
// local-midnight instant itself would otherwise confuse date arithmetic.)
func DateLabel(t time.Time, loc *time.Location) time.Time {
	y, m, d := t.In(loc).Date()
	return time.Date(y, m, d, 0, 0, 0, 0, time.UTC)
}

// DateLabelOfDate converts a local-midnight value to its date-only UTC label.
func DateLabelOfDate(localMidnight time.Time) time.Time {
	y, m, d := localMidnight.Date()
	return time.Date(y, m, d, 0, 0, 0, 0, time.UTC)
}

// IsNight reports whether the instant t (UTC) falls inside the nightly window
// [startHour, endHour) in local wall-clock time. endHour <= startHour means the
// window wraps past midnight (e.g. 20 -> 6 covers 20:00..05:59:59).
//
// It also returns the night-owning local date: for a window that wraps, hours
// before endHour belong to the previous calendar day's window (so 02:00 on
// Jan 2 belongs to the night keyed by Jan 1).
func IsNight(t time.Time, loc *time.Location, startHour, endHour int) (bool, time.Time) {
	local := t.In(loc)
	hour := local.Hour()
	day := LocalDate(t, loc)

	if endHour <= startHour {
		// Wrapping window: [startHour,24) U [0,endHour)
		if hour >= startHour {
			return true, day
		}
		if hour < endHour {
			return true, day.AddDate(0, 0, -1)
		}
		return false, day
	}
	// Non-wrapping window [startHour,endHour)
	if hour >= startHour && hour < endHour {
		return true, day
	}
	return false, day
}

// NightBounds converts a local night-owning date + hours into the UTC interval
// [start,end) of that night. Handles wrapping windows and DST (building the
// end wall-clock time directly in the location lets Go apply the correct
// offset, so a fall-back night spans 11 UTC hours).
func NightBounds(day time.Time, startHour, endHour int) (time.Time, time.Time) {
	start := time.Date(day.Year(), day.Month(), day.Day(), startHour, 0, 0, 0, day.Location())
	if endHour <= startHour {
		// Wrapping window: end is endHour on the following local day.
		end := time.Date(day.Year(), day.Month(), day.Day()+1, endHour, 0, 0, 0, day.Location())
		return start.UTC(), end.UTC()
	}
	end := time.Date(day.Year(), day.Month(), day.Day(), endHour, 0, 0, 0, day.Location())
	return start.UTC(), end.UTC()
}

// BurstWindowStart floors t to a UTC tumbling window of width w. Windows align
// to the epoch (so 10-minute windows are :00/:10/:20/:30/:40/:50).
func BurstWindowStart(t time.Time, w time.Duration) time.Time {
	u := t.Unix()
	step := int64(w / time.Second)
	return time.Unix((u/step)*step, 0).UTC()
}

// MidnightUTC returns the UTC midnight on the given UTC date.
func MidnightUTC(t time.Time) time.Time {
	u := t.UTC()
	return time.Date(u.Year(), u.Month(), u.Day(), 0, 0, 0, 0, time.UTC)
}

// DayRange returns [00:00, next 00:00) UTC for the UTC calendar day of t.
func DayRange(t time.Time) (time.Time, time.Time) {
	s := MidnightUTC(t)
	return s, s.Add(24 * time.Hour)
}
