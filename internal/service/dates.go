package service

import (
	"time"

	"github.com/jackc/pgx/v5/pgtype"
)

// truncDate normalizes to UTC midnight; PostgreSQL dates are read back as UTC.
func truncDate(t time.Time) time.Time {
	y, m, d := t.UTC().Date()
	return time.Date(y, m, d, 0, 0, 0, 0, time.UTC)
}

func monthStart(t time.Time) time.Time {
	t = truncDate(t)
	return time.Date(t.Year(), t.Month(), 1, 0, 0, 0, 0, time.UTC)
}

func monthEnd(t time.Time) time.Time {
	return monthStart(t).AddDate(0, 1, 0).AddDate(0, 0, -1)
}

func addDays(t time.Time, n int) time.Time { return truncDate(t).AddDate(0, 0, n) }

func dateStr(t time.Time) string { return truncDate(t).Format("2006-01-02") }

func pgtypeDateNull() pgtype.Date { return pgtype.Date{Valid: false} }

func pgtypeDateOf(t time.Time) pgtype.Date {
	return pgtype.Date{Time: truncDate(t), Valid: true}
}
