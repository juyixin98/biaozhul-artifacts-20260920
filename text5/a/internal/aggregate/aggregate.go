// Package aggregate maintains daily and weekly summaries.
//
// Every update — incremental ingest, late backfill, or a full rebuild — uses
// the same primitive: recompute one employee-day (or department-week) from
// raw_snapshots and UPSERT the result, serialized by a per-key advisory
// transaction lock. Because updates are full recomputes rather than deltas,
// incremental processing and rebuilding from raw are identical by
// construction, and concurrent writers cannot lose or double-count rows.
package aggregate

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"time"

	"github.com/jmoiron/sqlx"
)

// ErrRangePurged is returned when a rebuild range reaches into history whose
// raw data has been cleaned up; recomputing there would replace complete
// summaries with partial ones.
var ErrRangePurged = errors.New("rebuild range includes purged history")

// WeekStart returns the ISO Monday of the week containing d.
func WeekStart(d time.Time) time.Time {
	d = time.Date(d.Year(), d.Month(), d.Day(), 0, 0, 0, 0, time.UTC)
	return d.AddDate(0, 0, -(int(d.Weekday())+6)%7)
}

// RecomputeDailyTx recomputes one employee-day from raw_snapshots inside tx.
// If no raw rows exist for the day the existing summary is left untouched, so
// cleaning up raw data never erases its summaries.
func RecomputeDailyTx(ctx context.Context, tx *sqlx.Tx, employeeID int64, day time.Time) error {
	if _, err := tx.ExecContext(ctx,
		`SELECT pg_advisory_xact_lock(hashtextextended($1, 0))`,
		fmt.Sprintf("daily:%d:%s", employeeID, day.Format("2006-01-02"))); err != nil {
		return err
	}
	_, err := tx.ExecContext(ctx, `
		INSERT INTO daily_summaries AS ds
			(employee_id, day, productive_count, unproductive_count, neutral_count, total_count, snapshots, rebuilt_at)
		SELECT employee_id, local_date,
			COALESCE(SUM(activity_count) FILTER (WHERE category = 'productive'), 0),
			COALESCE(SUM(activity_count) FILTER (WHERE category = 'unproductive'), 0),
			COALESCE(SUM(activity_count) FILTER (WHERE category = 'neutral'), 0),
			COALESCE(SUM(activity_count), 0),
			COUNT(*),
			now()
		FROM raw_snapshots
		WHERE employee_id = $1 AND local_date = $2
		GROUP BY employee_id, local_date
		ON CONFLICT (employee_id, day) DO UPDATE SET
			productive_count   = EXCLUDED.productive_count,
			unproductive_count = EXCLUDED.unproductive_count,
			neutral_count      = EXCLUDED.neutral_count,
			total_count        = EXCLUDED.total_count,
			snapshots          = EXCLUDED.snapshots,
			rebuilt_at         = EXCLUDED.rebuilt_at`,
		employeeID, day.Format("2006-01-02"))
	return err
}

// RecomputeWeeklyTx recomputes one department-week from daily_summaries.
func RecomputeWeeklyTx(ctx context.Context, tx *sqlx.Tx, departmentID int64, weekStart time.Time) error {
	if _, err := tx.ExecContext(ctx,
		`SELECT pg_advisory_xact_lock(hashtextextended($1, 0))`,
		fmt.Sprintf("weekly:%d:%s", departmentID, weekStart.Format("2006-01-02"))); err != nil {
		return err
	}
	_, err := tx.ExecContext(ctx, `
		INSERT INTO weekly_summaries AS ws
			(department_id, week_start, productive_count, unproductive_count, neutral_count, total_count, employees, rebuilt_at)
		SELECT e.department_id, $2::date,
			COALESCE(SUM(ds.productive_count), 0),
			COALESCE(SUM(ds.unproductive_count), 0),
			COALESCE(SUM(ds.neutral_count), 0),
			COALESCE(SUM(ds.total_count), 0),
			COUNT(DISTINCT ds.employee_id),
			now()
		FROM daily_summaries ds
		JOIN employees e ON e.id = ds.employee_id
		WHERE e.department_id = $1
		  AND ds.day >= $2::date AND ds.day < $2::date + 7
		GROUP BY e.department_id
		ON CONFLICT (department_id, week_start) DO UPDATE SET
			productive_count   = EXCLUDED.productive_count,
			unproductive_count = EXCLUDED.unproductive_count,
			neutral_count      = EXCLUDED.neutral_count,
			total_count        = EXCLUDED.total_count,
			employees          = EXCLUDED.employees,
			rebuilt_at         = EXCLUDED.rebuilt_at`,
		departmentID, weekStart.Format("2006-01-02"))
	return err
}

// Rebuild recomputes every employee-day in [from, to] that still has raw
// data, then every department-week overlapping the range. Each key is
// recomputed in its own short transaction under the same advisory locks used
// by ingestion, so a rebuild can run concurrently with live ingest without
// losing or double-counting writes.
func Rebuild(ctx context.Context, db *sqlx.DB, from, to time.Time) (days int, err error) {
	var retainedFrom *time.Time
	if err := db.GetContext(ctx, &retainedFrom,
		`SELECT raw_retained_from FROM system_state WHERE id = TRUE`); err != nil {
		return 0, err
	}
	if retainedFrom != nil && from.Before(*retainedFrom) {
		return 0, fmt.Errorf("%w: raw data before %s has been cleaned up",
			ErrRangePurged, retainedFrom.Format("2006-01-02"))
	}

	type empDay struct {
		EmployeeID int64     `db:"employee_id"`
		Day        time.Time `db:"local_date"`
	}
	var empDays []empDay
	if err := db.SelectContext(ctx, &empDays, `
		SELECT DISTINCT employee_id, local_date FROM raw_snapshots
		WHERE local_date >= $1 AND local_date <= $2
		ORDER BY employee_id, local_date`,
		from.Format("2006-01-02"), to.Format("2006-01-02")); err != nil {
		return 0, err
	}

	for _, ed := range empDays {
		if err := withTx(ctx, db, func(tx *sqlx.Tx) error {
			return RecomputeDailyTx(ctx, tx, ed.EmployeeID, ed.Day)
		}); err != nil {
			return days, err
		}
		days++
	}

	type deptWeek struct {
		DepartmentID int64     `db:"department_id"`
		WeekStart    time.Time `db:"week_start"`
	}
	var deptWeeks []deptWeek
	if err := db.SelectContext(ctx, &deptWeeks, `
		SELECT DISTINCT e.department_id,
			date_trunc('week', ds.day)::date AS week_start
		FROM daily_summaries ds
		JOIN employees e ON e.id = ds.employee_id
		WHERE ds.day >= $1 AND ds.day <= $2`,
		from.Format("2006-01-02"), to.Format("2006-01-02")); err != nil {
		return days, err
	}
	for _, dw := range deptWeeks {
		if err := withTx(ctx, db, func(tx *sqlx.Tx) error {
			return RecomputeWeeklyTx(ctx, tx, dw.DepartmentID, dw.WeekStart)
		}); err != nil {
			return days, err
		}
	}
	return days, nil
}

// Cleanup deletes raw snapshots strictly before `before` and advances the
// retention watermark. Summaries are never touched; rebuilds reaching before
// the watermark are refused from then on.
func Cleanup(ctx context.Context, db *sqlx.DB, before time.Time) (deleted int64, err error) {
	err = withTx(ctx, db, func(tx *sqlx.Tx) error {
		res, err := tx.ExecContext(ctx,
			`DELETE FROM raw_snapshots WHERE local_date < $1`, before.Format("2006-01-02"))
		if err != nil {
			return err
		}
		if deleted, err = res.RowsAffected(); err != nil {
			return err
		}
		_, err = tx.ExecContext(ctx, `
			UPDATE system_state
			SET raw_retained_from = GREATEST(COALESCE(raw_retained_from, $1::date), $1::date)
			WHERE id = TRUE`, before.Format("2006-01-02"))
		return err
	})
	return deleted, err
}

// RetainedFrom reports the earliest local_date whose raw data is complete,
// or nil if nothing has been cleaned up yet.
func RetainedFrom(ctx context.Context, db *sqlx.DB) (*time.Time, error) {
	var retainedFrom *time.Time
	err := db.GetContext(ctx, &retainedFrom,
		`SELECT raw_retained_from FROM system_state WHERE id = TRUE`)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	return retainedFrom, err
}

func withTx(ctx context.Context, db *sqlx.DB, fn func(*sqlx.Tx) error) error {
	tx, err := db.BeginTxx(ctx, nil)
	if err != nil {
		return err
	}
	if err := fn(tx); err != nil {
		tx.Rollback()
		return err
	}
	return tx.Commit()
}
