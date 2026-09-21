package timeutil

import "time"

// TruncateMinuteUTC rounds a timestamp down to the minute in UTC. Agents send
// one snapshot per minute and (workstation, minute, app) is the idempotency
// key, so seconds are never significant.
func TruncateMinuteUTC(t time.Time) time.Time {
	t = t.UTC()
	return time.Date(t.Year(), t.Month(), t.Day(), t.Hour(), t.Minute(), 0, 0, time.UTC)
}

// LocalDate is the calendar date on which the minute falls in the employee's
// own timezone. This drives cross-day handling: a 23:30 UTC minute lands on
// different local dates in New York versus Tokyo.
func LocalDate(t time.Time, loc *time.Location) time.Time {
	lt := t.In(loc)
	return time.Date(lt.Year(), lt.Month(), lt.Day(), 0, 0, 0, 0, time.UTC)
}

// MondayOfWeek returns the UTC-midnight Monday of the ISO week containing the
// local date.
func MondayOfWeek(localDate time.Time) time.Time {
	dow := int(localDate.Weekday())
	if dow == 0 {
		dow = 7
	}
	return localDate.AddDate(0, 0, -(dow - 1))
}

// MinuteOfDay returns minutes since local midnight for the timestamp in loc.
func MinuteOfDay(t time.Time, loc *time.Location) int {
	lt := t.In(loc)
	return lt.Hour()*60 + lt.Minute()
}
