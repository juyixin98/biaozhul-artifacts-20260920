package repo

import (
	"context"
	"time"
)

type SummaryFilter struct {
	DepartmentID *int64
	EmployeeID   *int64
	From         *time.Time
	To           *time.Time
	Limit        int
}

func (r *Repo) ListDaily(ctx context.Context, f SummaryFilter) ([]DailySummary, error) {
	q := `SELECT s.* FROM employee_daily_summary s
	       JOIN employees e ON e.id = s.employee_id WHERE 1=1`
	args := []any{}
	if f.DepartmentID != nil {
		q += " AND s.department_id = ?"
		args = append(args, *f.DepartmentID)
	}
	if f.EmployeeID != nil {
		q += " AND s.employee_id = ?"
		args = append(args, *f.EmployeeID)
	}
	if f.From != nil {
		q += " AND s.local_date >= ?"
		args = append(args, *f.From)
	}
	if f.To != nil {
		q += " AND s.local_date <= ?"
		args = append(args, *f.To)
	}
	q += " ORDER BY s.local_date, s.employee_id LIMIT ?"
	limit := f.Limit
	if limit <= 0 || limit > 10000 {
		limit = 10000
	}
	args = append(args, limit)
	var rows []DailySummary
	return rows, r.db.SelectContext(ctx, &rows, r.db.Rebind(q), args...)
}

func (r *Repo) ListWeekly(ctx context.Context, f SummaryFilter) ([]WeeklySummary, error) {
	q := `SELECT * FROM department_weekly_summary WHERE 1=1`
	args := []any{}
	if f.DepartmentID != nil {
		q += " AND department_id = ?"
		args = append(args, *f.DepartmentID)
	}
	if f.From != nil {
		q += " AND iso_week >= ?"
		args = append(args, *f.From)
	}
	if f.To != nil {
		q += " AND iso_week <= ?"
		args = append(args, *f.To)
	}
	q += " ORDER BY iso_week, department_id LIMIT ?"
	limit := f.Limit
	if limit <= 0 || limit > 10000 {
		limit = 10000
	}
	args = append(args, limit)
	var rows []WeeklySummary
	return rows, r.db.SelectContext(ctx, &rows, r.db.Rebind(q), args...)
}

// PurgeRaw deletes raw snapshots with minute_utc < cutoff. Summaries are
// retained; coverage for every touched employee/day is marked 'partial' with
// the count of minutes that survive, so a later rebuild can detect the gap
// and refuse to overwrite the still-complete summary with partial data.
func (r *Repo) PurgeRaw(ctx context.Context, cutoff time.Time) (int64, error) {
	tx, err := r.db.BeginTxx(ctx, nil)
	if err != nil {
		return 0, err
	}
	defer tx.Rollback()

	if _, err := tx.ExecContext(ctx, `
UPDATE employee_day_coverage c
   SET raw_state = 'partial',
       raw_minutes = (
           SELECT COUNT(DISTINCT s.minute_utc)
             FROM activity_snapshots s
            WHERE s.employee_id = c.employee_id
              AND s.local_date = c.local_date
              AND s.minute_utc >= $1),
       purged_at = now()
 WHERE EXISTS (
           SELECT 1 FROM activity_snapshots s
            WHERE s.employee_id = c.employee_id
              AND s.local_date = c.local_date
              AND s.minute_utc < $1)`, cutoff); err != nil {
		return 0, err
	}

	res, err := tx.ExecContext(ctx,
		`DELETE FROM activity_snapshots WHERE minute_utc < $1`, cutoff)
	if err != nil {
		return 0, err
	}
	purged, err := res.RowsAffected()
	if err != nil {
		return 0, err
	}
	if _, err := tx.ExecContext(ctx,
		`INSERT INTO raw_retention_runs (older_than_utc, purged_rows) VALUES ($1, $2)`,
		cutoff, purged); err != nil {
		return 0, err
	}
	return purged, tx.Commit()
}

type RetentionRun struct {
	ID           int64     `db:"id" json:"id"`
	OlderThanUTC time.Time `db:"older_than_utc" json:"older_than_utc"`
	PurgedRows   int64     `db:"purged_rows" json:"purged_rows"`
	RanAt        time.Time `db:"ran_at" json:"ran_at"`
}

// RetentionBounds describes the window in which raw data still exists and a
// rebuild is therefore possible.
type RetentionBounds struct {
	OldestMinute *time.Time `db:"oldest_minute" json:"oldest_minute"`
	NewestMinute *time.Time `db:"newest_minute" json:"newest_minute"`
}

func (r *Repo) RetentionBounds(ctx context.Context) (RetentionBounds, error) {
	var b RetentionBounds
	if err := r.db.GetContext(ctx, &b,
		`SELECT MIN(minute_utc) AS oldest_minute,
		        MAX(minute_utc) AS newest_minute
		   FROM activity_snapshots`); err != nil {
		return RetentionBounds{}, err
	}
	return b, nil
}

func (r *Repo) ListRetentionRuns(ctx context.Context) ([]RetentionRun, error) {
	var runs []RetentionRun
	return runs, r.db.SelectContext(ctx, &runs,
		`SELECT * FROM raw_retention_runs ORDER BY ran_at DESC LIMIT 50`)
}
