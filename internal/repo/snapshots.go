package repo

import (
	"context"
	"time"

	"github.com/jmoiron/sqlx"
)

// StoredRow is the result of looking up an idempotency key already in raw.
type StoredRow struct {
	WorkstationID int64     `db:"workstation_id"`
	MinuteUTC     time.Time `db:"minute_utc"`
	AppName       string    `db:"app_name"`
	ActivityCount int       `db:"activity_count"`
}

// FindStored returns existing raw rows matching any of the given keys, via a
// single unnest join (the composite IN clause is awkward through sqlx).
func (r *Repo) FindStored(ctx context.Context, tx sqlx.ExtContext, items []RawSnapshot) ([]StoredRow, error) {
	if len(items) == 0 {
		return nil, nil
	}
	wss := make([]int64, len(items))
	mins := make([]time.Time, len(items))
	apps := make([]string, len(items))
	for i, it := range items {
		wss[i] = it.WorkstationID
		mins[i] = it.MinuteUTC
		apps[i] = it.AppName
	}
	var rows []StoredRow
	err := sqlx.SelectContext(ctx, tx, &rows,
		`SELECT s.workstation_id, s.minute_utc, s.app_name, s.activity_count
		   FROM activity_snapshots s
		   JOIN unnest($1::bigint[], $2::timestamptz[], $3::text[])
		        AS t(ws, min, app)
		     ON s.workstation_id = t.ws AND s.minute_utc = t.min AND s.app_name = t.app`,
		wss, mins, apps)
	if err != nil {
		return nil, err
	}
	return rows, nil
}

// InsertRaw persists one resolved snapshot and marks its employee/day raw
// coverage as 'full'.
func (r *Repo) InsertRaw(ctx context.Context, tx sqlx.ExtContext, s RawSnapshot) error {
	_, err := tx.ExecContext(ctx,
		`INSERT INTO activity_snapshots
		 (workstation_id, employee_id, department_id, minute_utc, app_name,
		  activity_count, policy_version, classif_version, category, local_date)
		 VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)`,
		s.WorkstationID, s.EmployeeID, s.DepartmentID, s.MinuteUTC, s.AppName,
		s.ActivityCount, s.PolicyVersion, s.ClassifVersion, s.Category,
		s.LocalDate)
	if err != nil {
		return err
	}
	_, err = tx.ExecContext(ctx,
		`INSERT INTO employee_day_coverage (employee_id, local_date, raw_state, raw_minutes)
		 VALUES ($1, $2, 'full', 1)
		 ON CONFLICT (employee_id, local_date) DO UPDATE
		   SET raw_state = 'full',
		       raw_minutes = CASE WHEN employee_day_coverage.raw_state = 'full'
		                          THEN employee_day_coverage.raw_minutes + 1
		                          ELSE employee_day_coverage.raw_minutes END`,
		s.EmployeeID, s.LocalDate)
	return err
}

// RawFilter narrows detail/export listing; a manager only ever sees their own
// department.
type RawFilter struct {
	DepartmentID *int64
	EmployeeID   *int64
	From         *time.Time
	To           *time.Time
	Limit        int
}

// ListRaw returns raw activity rows ordered by minute. Used by both the
// detail endpoint and CSV export, so isolation is enforced in one place.
func (r *Repo) ListRaw(ctx context.Context, f RawFilter) ([]RawSnapshot, error) {
	q := `SELECT * FROM activity_snapshots WHERE 1=1`
	args := []any{}
	i := 1
	if f.DepartmentID != nil {
		q += " AND department_id = ?"
		args = append(args, *f.DepartmentID)
		i++
	}
	if f.EmployeeID != nil {
		q += " AND employee_id = ?"
		args = append(args, *f.EmployeeID)
		i++
	}
	if f.From != nil {
		q += " AND minute_utc >= ?"
		args = append(args, *f.From)
		i++
	}
	if f.To != nil {
		q += " AND minute_utc < ?"
		args = append(args, *f.To)
		i++
	}
	q += " ORDER BY minute_utc, employee_id, app_name"
	limit := f.Limit
	if limit <= 0 || limit > 10000 {
		limit = 10000
	}
	q += " LIMIT ?"
	args = append(args, limit)

	var rows []RawSnapshot
	err := r.db.SelectContext(ctx, &rows, r.db.Rebind(q), args...)
	if err != nil {
		return nil, err
	}
	return rows, nil
}
