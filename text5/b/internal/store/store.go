// Package store contains all persistence and aggregation logic.
//
// Aggregation strategy: every summary row is always a full recomputation of
// the underlying raw rows for its (employee, day) or (department, week)
// scope, written with INSERT ... ON CONFLICT DO UPDATE inside the same
// transaction as the raw writes. Because recomputation is total per scope
// (never an incremental "+="), incremental ingestion and a from-scratch
// rebuild are identical by construction, and concurrent writers serialize
// on the summary row's unique-key lock without losing or double-counting
// data.
package store

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"errors"
	"fmt"
	"time"

	"github.com/jmoiron/sqlx"
	"github.com/lib/pq"

	"desklens/internal/classify"
	"desklens/internal/model"
	"desklens/internal/policy"
)

// ErrValidation marks a malformed batch; the whole batch is rolled back.
type ErrValidation struct{ Msg string }

func (e ErrValidation) Error() string { return e.Msg }

// ErrConflict marks a re-delivered idempotency key carrying different
// content; the whole batch is rolled back.
type ErrConflict struct{ Msg string }

func (e ErrConflict) Error() string { return e.Msg }

// ErrBeforeCutoff marks a rebuild request reaching into cleaned-up history.
type ErrBeforeCutoff struct{ Msg string }

func (e ErrBeforeCutoff) Error() string { return e.Msg }

type Store struct {
	db *sqlx.DB
}

func New(db *sqlx.DB) *Store { return &Store{db: db} }

func (s *Store) DB() *sqlx.DB { return s.db }

// ---------------------------------------------------------------------------
// Ingestion
// ---------------------------------------------------------------------------

// IngestBatch validates, privacy-filters, classifies and persists a batch of
// snapshots, then recomputes exactly the affected employee-days and
// department-weeks. The whole batch is one transaction: any validation or
// conflict error rolls everything back.
func (s *Store) IngestBatch(ctx context.Context, inputs []model.SnapshotInput) (*model.IngestResult, error) {
	if len(inputs) == 0 {
		return nil, ErrValidation{Msg: "batch must contain at least one snapshot"}
	}

	tx, err := s.db.BeginTxx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()

	pol, err := currentPolicy(ctx, tx)
	if err != nil {
		return nil, err
	}
	classVer, rules, err := currentClassification(ctx, tx)
	if err != nil {
		return nil, err
	}

	res := &model.IngestResult{Received: len(inputs)}
	empCache := map[int64]empCtx{}
	type empDay struct {
		empID int64
		day   time.Time
	}
	affected := map[empDay]struct{}{}

	for i, in := range inputs {
		if in.WorkstationID == "" || in.AppName == "" {
			return nil, ErrValidation{Msg: fmt.Sprintf("snapshot %d: workstation_id and app_name are required", i)}
		}
		if in.ActivityCount < 0 {
			return nil, ErrValidation{Msg: fmt.Sprintf("snapshot %d: activity_count must be >= 0", i)}
		}
		if in.CapturedAt.IsZero() {
			return nil, ErrValidation{Msg: fmt.Sprintf("snapshot %d: captured_at is required", i)}
		}
		minute := in.CapturedAt.UTC().Truncate(time.Minute)

		ec, ok := empCache[in.EmployeeID]
		if !ok {
			ec, err = loadEmployee(ctx, tx, in.EmployeeID)
			if err != nil {
				if errors.Is(err, sql.ErrNoRows) {
					return nil, ErrValidation{Msg: fmt.Sprintf("snapshot %d: unknown employee_id %d", i, in.EmployeeID)}
				}
				return nil, err
			}
			empCache[in.EmployeeID] = ec
		}

		// Privacy gate: filtered data never reaches the raw table.
		if !pol.Allows(ec.emp.DepartmentID, ec.loc, minute, in.AppName) {
			res.Filtered++
			continue
		}

		cat := classify.Classify(rules, in.AppName)
		hash := contentHash(in, minute)

		var id int64
		err := tx.QueryRowContext(ctx, `
			INSERT INTO raw_snapshots
				(workstation_id, employee_id, minute_utc, app_name, activity_count,
				 content_hash, category, policy_version, classification_version)
			VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
			ON CONFLICT (workstation_id, minute_utc) DO NOTHING
			RETURNING id`,
			in.WorkstationID, in.EmployeeID, minute, in.AppName, in.ActivityCount,
			hash, cat, pol.Version, classVer).Scan(&id)
		if errors.Is(err, sql.ErrNoRows) {
			// Key already present: identical content is a no-op duplicate,
			// different content is an explicit conflict.
			var existing string
			if err := tx.QueryRowContext(ctx,
				`SELECT content_hash FROM raw_snapshots WHERE workstation_id = $1 AND minute_utc = $2`,
				in.WorkstationID, minute).Scan(&existing); err != nil {
				return nil, err
			}
			if existing == hash {
				res.Duplicates++
				continue
			}
			return nil, ErrConflict{Msg: fmt.Sprintf(
				"snapshot %d: workstation %s already reported different content for minute %s",
				i, in.WorkstationID, minute.Format(time.RFC3339))}
		}
		if err != nil {
			return nil, err
		}

		res.Inserted++
		affected[empDay{ec.emp.ID, localDay(minute, ec.loc)}] = struct{}{}
	}

	// Late snapshots only touch the days they belong to.
	for ad := range affected {
		ec := empCache[ad.empID]
		if err := recomputeDay(ctx, tx, ad.empID, ec.emp.DepartmentID, ec.emp.Timezone, ad.day); err != nil {
			return nil, err
		}
	}

	if err := tx.Commit(); err != nil {
		return nil, err
	}
	return res, nil
}

func contentHash(in model.SnapshotInput, minute time.Time) string {
	h := sha256.Sum256([]byte(fmt.Sprintf("%d|%s|%s|%d",
		in.EmployeeID, minute.Format(time.RFC3339), in.AppName, in.ActivityCount)))
	return hex.EncodeToString(h[:])
}

type empCtx struct {
	emp model.Employee
	loc *time.Location
}

func loadEmployee(ctx context.Context, tx *sqlx.Tx, id int64) (empCtx, error) {
	var emp model.Employee
	if err := tx.GetContext(ctx, &emp,
		`SELECT id, department_id, name, timezone FROM employees WHERE id = $1`, id); err != nil {
		return empCtx{}, err
	}
	loc, err := time.LoadLocation(emp.Timezone)
	if err != nil {
		return empCtx{}, fmt.Errorf("employee %d: bad timezone %q: %w", id, emp.Timezone, err)
	}
	return empCtx{emp: emp, loc: loc}, nil
}

func localDay(t time.Time, loc *time.Location) time.Time {
	l := t.In(loc)
	return time.Date(l.Year(), l.Month(), l.Day(), 0, 0, 0, 0, time.UTC)
}

// weekStart returns the Monday of the ISO week containing day (a UTC-midnight
// date value).
func weekStart(day time.Time) time.Time {
	wd := int(day.Weekday())
	if wd == 0 {
		wd = 7
	}
	return day.AddDate(0, 0, -(wd - 1))
}

// ---------------------------------------------------------------------------
// Aggregation
// ---------------------------------------------------------------------------

// recomputeDay fully recomputes one employee's daily summary from raw rows
// and upserts it, then recomputes the containing department week.
func recomputeDay(ctx context.Context, tx *sqlx.Tx, empID, deptID int64, tz string, day time.Time) error {
	type bucket struct {
		Category string `db:"category"`
		Sum      int64  `db:"sum"`
	}
	var buckets []bucket
	if err := tx.SelectContext(ctx, &buckets, `
		SELECT category, SUM(activity_count) AS sum
		FROM raw_snapshots
		WHERE employee_id = $1
		  AND (minute_utc AT TIME ZONE $2)::date = $3::date
		GROUP BY category`, empID, tz, day); err != nil {
		return err
	}

	var prod, unprod, neut int64
	for _, b := range buckets {
		switch b.Category {
		case classify.Productive:
			prod = b.Sum
		case classify.Unproductive:
			unprod = b.Sum
		case classify.Neutral:
			neut = b.Sum
		}
	}
	total := prod + unprod + neut

	if _, err := tx.ExecContext(ctx, `
		INSERT INTO employee_daily_summary (employee_id, day, productive, unproductive, neutral, total, updated_at)
		VALUES ($1, $2, $3, $4, $5, $6, now())
		ON CONFLICT (employee_id, day) DO UPDATE SET
			productive = EXCLUDED.productive,
			unproductive = EXCLUDED.unproductive,
			neutral = EXCLUDED.neutral,
			total = EXCLUDED.total,
			updated_at = now()`,
		empID, day, prod, unprod, neut, total); err != nil {
		return err
	}

	return recomputeWeek(ctx, tx, deptID, weekStart(day))
}

// recomputeWeek rebuilds one department-week from the daily summaries.
func recomputeWeek(ctx context.Context, tx *sqlx.Tx, deptID int64, monday time.Time) error {
	_, err := tx.ExecContext(ctx, `
		INSERT INTO department_weekly_summary (department_id, week_start, productive, unproductive, neutral, total, updated_at)
		SELECT $1, $2,
		       COALESCE(SUM(s.productive), 0),
		       COALESCE(SUM(s.unproductive), 0),
		       COALESCE(SUM(s.neutral), 0),
		       COALESCE(SUM(s.total), 0),
		       now()
		FROM employee_daily_summary s
		JOIN employees e ON e.id = s.employee_id
		WHERE e.department_id = $1
		  AND s.day >= $2::date
		  AND s.day <  $2::date + 7
		ON CONFLICT (department_id, week_start) DO UPDATE SET
			productive = EXCLUDED.productive,
			unproductive = EXCLUDED.unproductive,
			neutral = EXCLUDED.neutral,
			total = EXCLUDED.total,
			updated_at = now()`, deptID, monday)
	return err
}

// Rebuild recomputes every employee-day (and the containing weeks) that has
// raw data in the local-date range [from, to]. Ranges reaching before the
// retention cutoff are refused so partial history can never overwrite
// complete statistics. New ingestion during a rebuild is safe: both paths
// fully recompute their scopes inside transactions that serialize on the
// summary rows.
func (s *Store) Rebuild(ctx context.Context, from, to time.Time) (int, error) {
	if to.Before(from) {
		return 0, ErrValidation{Msg: "rebuild: 'to' must not be before 'from'"}
	}
	cutoff, err := s.RetentionCutoff(ctx)
	if err != nil {
		return 0, err
	}
	if cutoff != nil && from.Before(*cutoff) {
		return 0, ErrBeforeCutoff{Msg: fmt.Sprintf(
			"rebuild range starts %s but raw data before %s has been cleaned up; refusing to overwrite complete statistics with partial history",
			from.Format("2006-01-02"), cutoff.Format(time.RFC3339))}
	}

	tx, err := s.db.BeginTxx(ctx, nil)
	if err != nil {
		return 0, err
	}
	defer tx.Rollback()

	type pair struct {
		EmployeeID int64     `db:"employee_id"`
		Day        time.Time `db:"day"`
	}
	var pairs []pair
	if err := tx.SelectContext(ctx, &pairs, `
		SELECT r.employee_id, (r.minute_utc AT TIME ZONE e.timezone)::date AS day
		FROM raw_snapshots r
		JOIN employees e ON e.id = r.employee_id
		WHERE (r.minute_utc AT TIME ZONE e.timezone)::date BETWEEN $1::date AND $2::date
		GROUP BY r.employee_id, day
		ORDER BY r.employee_id, day`, from, to); err != nil {
		return 0, err
	}

	tzs := map[int64]string{}
	depts := map[int64]int64{}
	for _, p := range pairs {
		if _, found := tzs[p.EmployeeID]; !found {
			var emp model.Employee
			if err := tx.GetContext(ctx, &emp,
				`SELECT id, department_id, name, timezone FROM employees WHERE id = $1`, p.EmployeeID); err != nil {
				return 0, err
			}
			tzs[p.EmployeeID], depts[p.EmployeeID] = emp.Timezone, emp.DepartmentID
		}
		if err := recomputeDay(ctx, tx, p.EmployeeID, depts[p.EmployeeID], tzs[p.EmployeeID], p.Day); err != nil {
			return 0, err
		}
	}

	if err := tx.Commit(); err != nil {
		return 0, err
	}
	return len(pairs), nil
}

// Cleanup deletes raw snapshots strictly before `before` and advances the
// retention cutoff. Summaries are never touched.
func (s *Store) Cleanup(ctx context.Context, before time.Time) (int64, error) {
	tx, err := s.db.BeginTxx(ctx, nil)
	if err != nil {
		return 0, err
	}
	defer tx.Rollback()

	res, err := tx.ExecContext(ctx, `DELETE FROM raw_snapshots WHERE minute_utc < $1`, before)
	if err != nil {
		return 0, err
	}
	deleted, err := res.RowsAffected()
	if err != nil {
		return 0, err
	}
	if _, err := tx.ExecContext(ctx, `
		UPDATE retention_state
		SET raw_cutoff = GREATEST(COALESCE(raw_cutoff, '-infinity'::timestamptz), $1)
		WHERE id = TRUE`, before); err != nil {
		return 0, err
	}
	return deleted, tx.Commit()
}

// RetentionCutoff returns the oldest timestamp still rebuildable, or nil if
// no cleanup has ever run.
func (s *Store) RetentionCutoff(ctx context.Context) (*time.Time, error) {
	var cutoff *time.Time
	if err := s.db.GetContext(ctx, &cutoff, `SELECT raw_cutoff FROM retention_state WHERE id = TRUE`); err != nil {
		return nil, err
	}
	return cutoff, nil
}

// ---------------------------------------------------------------------------
// Policy & classification administration
// ---------------------------------------------------------------------------

func currentPolicy(ctx context.Context, tx *sqlx.Tx) (policy.Policy, error) {
	var row struct {
		Version      int            `db:"version"`
		StartMin     int            `db:"work_start_minutes"`
		EndMin       int            `db:"work_end_minutes"`
		Workdays     pq.Int64Array  `db:"workdays"`
		ExcludedApps pq.StringArray `db:"excluded_apps"`
		ExemptDepts  pq.Int64Array  `db:"exempt_department_ids"`
	}
	err := tx.GetContext(ctx, &row, `
		SELECT version, work_start_minutes, work_end_minutes, workdays, excluded_apps, exempt_department_ids
		FROM policy_versions ORDER BY version DESC LIMIT 1`)
	if errors.Is(err, sql.ErrNoRows) {
		return policy.Policy{}, ErrValidation{Msg: "no monitoring policy has been published"}
	}
	if err != nil {
		return policy.Policy{}, err
	}
	return policy.New(row.Version, row.StartMin, row.EndMin, row.Workdays, row.ExcludedApps, row.ExemptDepts), nil
}

func currentClassification(ctx context.Context, tx *sqlx.Tx) (int, []classify.Rule, error) {
	var ver int
	err := tx.GetContext(ctx, &ver, `SELECT COALESCE(MAX(version), 0) FROM classification_versions`)
	if err != nil {
		return 0, nil, err
	}
	if ver == 0 {
		return 0, nil, nil
	}
	var rules []classify.Rule
	if err := tx.SelectContext(ctx, &rules, `
		SELECT id, pattern, category, priority
		FROM classification_rules WHERE version = $1 ORDER BY id`, ver); err != nil {
		return 0, nil, err
	}
	return ver, rules, nil
}

// PublishPolicy inserts a new immutable policy version. It only affects
// snapshots ingested afterwards.
func (s *Store) PublishPolicy(ctx context.Context, startMin, endMin int, workdays []int64, excludedApps []string, exemptDepts []int64) (int, error) {
	if startMin < 0 || startMin >= 1440 || endMin <= 0 || endMin > 1440 || startMin >= endMin {
		return 0, ErrValidation{Msg: "invalid monitoring window"}
	}
	if len(workdays) == 0 {
		return 0, ErrValidation{Msg: "at least one workday is required"}
	}
	for _, d := range workdays {
		if d < 0 || d > 6 {
			return 0, ErrValidation{Msg: "workdays must be in 0..6"}
		}
	}
	if excludedApps == nil {
		excludedApps = []string{}
	}
	if exemptDepts == nil {
		exemptDepts = []int64{}
	}
	var ver int
	err := s.db.QueryRowContext(ctx, `
		INSERT INTO policy_versions (work_start_minutes, work_end_minutes, workdays, excluded_apps, exempt_department_ids)
		VALUES ($1, $2, $3, $4, $5) RETURNING version`,
		startMin, endMin, pq.Array(workdays), pq.Array(excludedApps), pq.Array(exemptDepts)).Scan(&ver)
	return ver, err
}

// PublishClassification inserts a new immutable classification version with
// its rule set. Existing raw rows keep the version they were classified with.
func (s *Store) PublishClassification(ctx context.Context, rules []classify.Rule) (int, error) {
	if len(rules) == 0 {
		return 0, ErrValidation{Msg: "at least one rule is required"}
	}
	for _, r := range rules {
		switch r.Category {
		case classify.Productive, classify.Unproductive, classify.Neutral:
		default:
			return 0, ErrValidation{Msg: fmt.Sprintf("invalid category %q", r.Category)}
		}
		if r.Pattern == "" {
			return 0, ErrValidation{Msg: "rule pattern must not be empty"}
		}
	}

	tx, err := s.db.BeginTxx(ctx, nil)
	if err != nil {
		return 0, err
	}
	defer tx.Rollback()

	var ver int
	if err := tx.QueryRowContext(ctx, `INSERT INTO classification_versions DEFAULT VALUES RETURNING version`).Scan(&ver); err != nil {
		return 0, err
	}
	for _, r := range rules {
		if _, err := tx.ExecContext(ctx, `
			INSERT INTO classification_rules (version, pattern, category, priority)
			VALUES ($1, $2, $3, $4)`, ver, r.Pattern, r.Category, r.Priority); err != nil {
			return 0, err
		}
	}
	return ver, tx.Commit()
}

// ---------------------------------------------------------------------------
// Read queries
// ---------------------------------------------------------------------------

func (s *Store) GetEmployee(ctx context.Context, id int64) (*model.Employee, error) {
	var emp model.Employee
	if err := s.db.GetContext(ctx, &emp,
		`SELECT id, department_id, name, timezone FROM employees WHERE id = $1`, id); err != nil {
		return nil, err
	}
	return &emp, nil
}

func (s *Store) DailySummaries(ctx context.Context, empID int64, from, to time.Time) ([]model.DailySummary, error) {
	var out []model.DailySummary
	err := s.db.SelectContext(ctx, &out, `
		SELECT employee_id, day, productive, unproductive, neutral, total
		FROM employee_daily_summary
		WHERE employee_id = $1 AND day BETWEEN $2::date AND $3::date
		ORDER BY day`, empID, from, to)
	return out, err
}

func (s *Store) WeeklySummaries(ctx context.Context, deptID int64, from, to time.Time) ([]model.WeeklySummary, error) {
	var out []model.WeeklySummary
	err := s.db.SelectContext(ctx, &out, `
		SELECT department_id, week_start, productive, unproductive, neutral, total
		FROM department_weekly_summary
		WHERE department_id = $1 AND week_start BETWEEN $2::date AND $3::date
		ORDER BY week_start`, deptID, from, to)
	return out, err
}

// SnapshotsForDay returns the raw detail rows for one employee-local day.
// Rows older than the retention cutoff no longer exist; callers can consult
// RetentionCutoff to distinguish "no activity" from "cleaned up".
func (s *Store) SnapshotsForDay(ctx context.Context, empID int64, tz string, day time.Time) ([]model.RawSnapshot, error) {
	var out []model.RawSnapshot
	err := s.db.SelectContext(ctx, &out, `
		SELECT id, workstation_id, employee_id, minute_utc, app_name, activity_count,
		       category, policy_version, classification_version
		FROM raw_snapshots
		WHERE employee_id = $1
		  AND (minute_utc AT TIME ZONE $2)::date = $3::date
		ORDER BY minute_utc`, empID, tz, day)
	return out, err
}

func (s *Store) UserByToken(ctx context.Context, token string) (*model.User, error) {
	var u model.User
	if err := s.db.GetContext(ctx, &u,
		`SELECT id, username, token, role, department_id FROM users WHERE token = $1`, token); err != nil {
		return nil, err
	}
	return &u, nil
}
