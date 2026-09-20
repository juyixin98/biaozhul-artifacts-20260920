package api

import (
	"fmt"
	"time"

	"github.com/jackc/pgx/v5/pgtype"
)

// ts converts a time.Time to a valid pgtype.Timestamptz.
func ts(t time.Time) pgtype.Timestamptz {
	return pgtype.Timestamptz{Time: t.UTC(), Valid: true}
}

// tsTime converts a pgtype.Timestamptz to a UTC time.Time.
func tsTime(v pgtype.Timestamptz) time.Time {
	if !v.Valid {
		return time.Time{}
	}
	return v.Time.UTC()
}

// date builds a pgtype.Date for a calendar date in the store's timezone.
// The value is stored as UTC midnight of that calendar day, which is how the
// pgx date codec round-trips a bare date.
func date(y int, m time.Month, d int) pgtype.Date {
	return pgtype.Date{Time: time.Date(y, m, d, 0, 0, 0, 0, time.UTC), Valid: true}
}

// localDay returns the calendar date of t in loc. Late-arriving events are
// bucketed by the date on which they actually occurred at the store, not by
// the UTC date or the time the server received them.
func localDay(t time.Time, loc *time.Location) (int, time.Month, int) {
	return t.In(loc).Date()
}

// parseOccurredAt parses an RFC3339 timestamp (which always carries either a Z
// or a numeric UTC offset) and normalizes it to UTC.
func parseOccurredAt(s string) (time.Time, error) {
	t, err := time.Parse(time.RFC3339Nano, s)
	if err != nil {
		return time.Time{}, fmt.Errorf("occurred_at must be an RFC3339 timestamp with UTC offset: %w", err)
	}
	return t.UTC(), nil
}
