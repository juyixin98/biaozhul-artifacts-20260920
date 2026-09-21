package repo

import (
	"context"
	"time"

	"github.com/jmoiron/sqlx"
)

const (
	advisoryNamespaceDaily  = 1
	advisoryNamespaceWeekly = 2
)

// LockDaily / LockWeekly take transaction-scoped advisory locks. Every
// summary key is recomputed under its lock, so concurrent ingestion batches
// touching the same day/week serialize and the last committer sees all raw
// rows — no lost updates, no double counting.
func LockDaily(ctx context.Context, tx sqlx.ExecerContext, employeeID int64, day time.Time) error {
	_, err := tx.ExecContext(ctx,
		`SELECT pg_advisory_xact_lock($1, hashtext($2))`,
		advisoryNamespaceDaily, lockKey(employeeID, day))
	return err
}

func LockWeekly(ctx context.Context, tx sqlx.ExecerContext, departmentID int64, week time.Time) error {
	_, err := tx.ExecContext(ctx,
		`SELECT pg_advisory_xact_lock($1, hashtext($2))`,
		advisoryNamespaceWeekly, lockKey(departmentID, week))
	return err
}

func lockKey(id int64, t time.Time) string {
	return time.Date(t.Year(), t.Month(), t.Day(), 0, 0, 0, 0, time.UTC).Format("2006-01-02") +
		"/" + itoa(id)
}

func itoa(i int64) string {
	if i == 0 {
		return "0"
	}
	neg := i < 0
	if neg {
		i = -i
	}
	var b [20]byte
	p := len(b)
	for i > 0 {
		p--
		b[p] = byte('0' + i%10)
		i /= 10
	}
	if neg {
		p--
		b[p] = '-'
	}
	return string(b[p:])
}

// RecomputeDaily rebuilds one employee/day summary from raw rows and upserts
// it. The caller must hold LockDaily for the key.
func (r *Repo) RecomputeDaily(ctx context.Context, tx sqlx.ExtContext, employeeID int64, day time.Time) error {
	_, err := tx.ExecContext(ctx, `
WITH agg AS (
    SELECT employee_id,
           local_date,
           SUM(activity_count) FILTER (WHERE category = 'productive')     AS productive_count,
           SUM(activity_count) FILTER (WHERE category = 'non_productive') AS non_productive_count,
           SUM(activity_count) FILTER (WHERE category = 'neutral')        AS neutral_count,
           COUNT(DISTINCT minute_utc)                                     AS active_minutes,
           MIN(policy_version)                                            AS policy_version,
           MIN(classif_version)                                           AS classif_version
      FROM activity_snapshots
     WHERE employee_id = $1 AND local_date = $2
     GROUP BY employee_id, local_date
), dep AS (
    SELECT department_id FROM activity_snapshots
     WHERE employee_id = $1 AND local_date = $2
     ORDER BY minute_utc DESC, id DESC LIMIT 1
)
INSERT INTO employee_daily_summary
    (employee_id, local_date, department_id, productive_count,
     non_productive_count, neutral_count, active_minutes,
     policy_version, classif_version, recomputed_at)
SELECT a.employee_id, a.local_date, d.department_id,
       COALESCE(a.productive_count, 0), COALESCE(a.non_productive_count, 0),
       COALESCE(a.neutral_count, 0), a.active_minutes,
       a.policy_version, a.classif_version, now()
  FROM agg a CROSS JOIN dep d
ON CONFLICT (employee_id, local_date) DO UPDATE SET
   department_id        = EXCLUDED.department_id,
   productive_count     = EXCLUDED.productive_count,
   non_productive_count = EXCLUDED.non_productive_count,
   neutral_count        = EXCLUDED.neutral_count,
   active_minutes       = EXCLUDED.active_minutes,
   policy_version       = EXCLUDED.policy_version,
   classif_version      = EXCLUDED.classif_version,
   recomputed_at        = now()`, employeeID, day)
	return err
}

// RecomputeWeekly rebuilds one department/week summary from raw rows.
// The caller must hold LockWeekly for the key.
func (r *Repo) RecomputeWeekly(ctx context.Context, tx sqlx.ExtContext, departmentID int64, week time.Time) error {
	_, err := tx.ExecContext(ctx, `
WITH days AS (
    SELECT employee_id,
           local_date,
           local_date - ((EXTRACT(ISODOW FROM local_date)::int) - 1) * INTERVAL '1 day' AS week_start,
           SUM(activity_count) FILTER (WHERE category = 'productive')     AS productive_count,
           SUM(activity_count) FILTER (WHERE category = 'non_productive') AS non_productive_count,
           SUM(activity_count) FILTER (WHERE category = 'neutral')        AS neutral_count
      FROM activity_snapshots
     WHERE department_id = $1
       AND local_date >= $2 AND local_date < $2 + 7
     GROUP BY employee_id, local_date
)
INSERT INTO department_weekly_summary
    (department_id, iso_week, productive_count, non_productive_count,
     neutral_count, active_employee_days, recomputed_at)
SELECT $1, $2,
       COALESCE(SUM(productive_count), 0),
       COALESCE(SUM(non_productive_count), 0),
       COALESCE(SUM(neutral_count), 0),
       COUNT(DISTINCT (employee_id, local_date)), now()
  FROM days
 WHERE week_start = $2
ON CONFLICT (department_id, iso_week) DO UPDATE SET
   productive_count     = EXCLUDED.productive_count,
   non_productive_count = EXCLUDED.non_productive_count,
   neutral_count        = EXCLUDED.neutral_count,
   active_employee_days = EXCLUDED.active_employee_days,
   recomputed_at        = now()`, departmentID, week)
	return err
}

// FullDayKeys returns all employee/day keys that still have full raw
// coverage — the safe rebuild set.
func (r *Repo) FullDayKeys(ctx context.Context) ([][2]any, error) {
	return r.dayKeysByState(ctx, "full")
}

// AllDayKeys returns every tracked employee/day key (full or partial).
func (r *Repo) AllDayKeys(ctx context.Context) ([][2]any, error) {
	return r.dayKeysByState(ctx, "")
}

func (r *Repo) dayKeysByState(ctx context.Context, state string) ([][2]any, error) {
	type key struct {
		EmployeeID int64     `db:"employee_id"`
		LocalDate  time.Time `db:"local_date"`
	}
	q := `SELECT employee_id, local_date FROM employee_day_coverage`
	args := []any{}
	if state != "" {
		q += " WHERE raw_state = $1"
		args = append(args, state)
	}
	q += " ORDER BY employee_id, local_date"
	var rows []key
	if err := r.db.SelectContext(ctx, &rows, q, args...); err != nil {
		return nil, err
	}
	keys := make([][2]any, len(rows))
	for i, k := range rows {
		keys[i] = [2]any{k.EmployeeID, k.LocalDate}
	}
	return keys, nil
}

// FullWeekKeys returns department/week pairs backed by raw rows.
func (r *Repo) FullWeekKeys(ctx context.Context) ([][2]any, error) {
	return r.weekKeys(ctx, false)
}

// AllWeekKeys also includes weeks that only survive in retained summaries
// (raw rows purged), so the rebuild report can mark them skipped.
func (r *Repo) AllWeekKeys(ctx context.Context) ([][2]any, error) {
	return r.weekKeys(ctx, true)
}

func (r *Repo) weekKeys(ctx context.Context, includeSummaries bool) ([][2]any, error) {
	type key struct {
		DepartmentID int64     `db:"department_id"`
		IsoWeek      time.Time `db:"iso_week"`
	}
	q := `
SELECT DISTINCT department_id,
       local_date - ((EXTRACT(ISODOW FROM local_date)::int) - 1) * INTERVAL '1 day' AS iso_week
  FROM activity_snapshots`
	if includeSummaries {
		q += `
UNION
SELECT department_id, iso_week FROM department_weekly_summary`
	}
	q += ` ORDER BY department_id, iso_week`
	var rows []key
	if err := r.db.SelectContext(ctx, &rows, q); err != nil {
		return nil, err
	}
	keys := make([][2]any, len(rows))
	for i, k := range rows {
		keys[i] = [2]any{k.DepartmentID, k.IsoWeek}
	}
	return keys, nil
}

// DayIsFull reports whether raw data for one employee/day is complete.
func (r *Repo) DayIsFull(ctx context.Context, tx sqlx.QueryerContext, employeeID int64, day time.Time) (bool, error) {
	var state string
	err := sqlx.GetContext(ctx, tx, &state,
		`SELECT raw_state FROM employee_day_coverage
		  WHERE employee_id = $1 AND local_date = $2`, employeeID, day)
	if err != nil {
		return false, err
	}
	return state == "full", nil
}

// FullDaysInWeek returns:
//   - covered: employee/day keys in raw whose coverage is still 'full';
//   - present: every employee/day key raw data covers.
//
// The gate additionally needs PurgePartialInWeek, because a day whose raw
// rows were fully purged vanishes from BOTH sets: raw rows alone cannot see
// the missing history.
func (r *Repo) FullDaysInWeek(ctx context.Context, tx sqlx.QueryerContext, departmentID int64, week time.Time) (covered, present map[[2]int64]bool, err error) {
	covered = map[[2]int64]bool{}
	present = map[[2]int64]bool{}

	type key struct {
		EmployeeID int64     `db:"employee_id"`
		LocalDate  time.Time `db:"local_date"`
	}
	var covRows []key
	if err = sqlx.SelectContext(ctx, tx, &covRows,
		`SELECT DISTINCT s.employee_id, s.local_date
		   FROM activity_snapshots s
		   JOIN employee_day_coverage c
		     ON c.employee_id = s.employee_id AND c.local_date = s.local_date
		  WHERE s.department_id = $1
		    AND s.local_date >= $2 AND s.local_date < $2 + 7
		    AND c.raw_state = 'full'`, departmentID, week); err != nil {
		return nil, nil, err
	}
	for _, k := range covRows {
		covered[[2]int64{k.EmployeeID, ordinalDate(k.LocalDate)}] = true
	}

	var rawRows []key
	if err = sqlx.SelectContext(ctx, tx, &rawRows,
		`SELECT DISTINCT employee_id, local_date FROM activity_snapshots
		  WHERE department_id = $1
		    AND local_date >= $2 AND local_date < $2 + 7`, departmentID, week); err != nil {
		return nil, nil, err
	}
	for _, k := range rawRows {
		present[[2]int64{k.EmployeeID, ordinalDate(k.LocalDate)}] = true
	}
	return covered, present, nil
}

// WeekHasPartial reports whether any tracked employee/day in the
// department's week has 'partial' raw coverage. Such days cannot be rebuilt
// (raw rows are gone) and block the whole-week rebuild so the retained
// weekly statistic is never replaced by a sum over the surviving remainder.
func (r *Repo) WeekHasPartial(ctx context.Context, tx sqlx.QueryerContext, departmentID int64, week time.Time) (bool, error) {
	var n int
	err := sqlx.GetContext(ctx, tx, &n,
		`SELECT count(*)
		   FROM employee_day_coverage c
		   JOIN employees e ON e.id = c.employee_id
		  WHERE e.department_id = $1
		    AND c.local_date >= $2 AND c.local_date < $2 + 7
		    AND c.raw_state = 'partial'`, departmentID, week)
	if err != nil {
		return false, err
	}
	return n > 0, nil
}

// ordinalDate packs a date into a single int64 for map keying.
func ordinalDate(t time.Time) int64 {
	return t.Unix() / 86400
}
