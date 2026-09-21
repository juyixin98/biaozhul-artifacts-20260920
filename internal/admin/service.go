// Package admin implements management endpoints: publishing policy and
// classification versions, rebuilding summaries from raw data, and purging raw
// data while keeping summaries.
package admin

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"net/http"
	"time"

	"github.com/jmoiron/sqlx"

	"desklens/internal/aggregate"
	"desklens/internal/model"
	"desklens/internal/pattern"
	"desklens/internal/timeutil"
)

const retentionLockKey int64 = 7701 // shared(ingest/rebuild) vs exclusive(purge)

type APIError struct {
	HTTPStatus int
	Code       string
	Message    string
}

func (e *APIError) Error() string { return fmt.Sprintf("%s: %s", e.Code, e.Message) }

func badRequest(code, msg string) *APIError {
	return &APIError{http.StatusBadRequest, code, msg}
}

type Service struct {
	DB *sqlx.DB
}

func New(db *sqlx.DB) *Service { return &Service{DB: db} }

// ---------------------------------------------------------------------------
// Policy publishing
// ---------------------------------------------------------------------------

type PolicyInput struct {
	MonitoringStart   string   `json:"monitoring_start"` // HH:MM local
	MonitoringEnd     string   `json:"monitoring_end"`
	ExcludedApps      []string `json:"excluded_apps"`
	ExemptDepartments []int64  `json:"exempt_departments"`
	Note              string   `json:"note"`
	PublishedBy       string   `json:"published_by"`
}

type VersionOut struct {
	Version     int32  `json:"version"`
	PublishedAt string `json:"published_at"`
}

// PublishPolicy inserts a new immutable policy version. It only affects future
// ingest; existing raw rows and summaries keep their stamped version.
func (s *Service) PublishPolicy(ctx context.Context, in PolicyInput) (*VersionOut, error) {
	start, err := parseHHMM(in.MonitoringStart)
	if err != nil {
		return nil, badRequest("invalid_window", "monitoring_start must be HH:MM")
	}
	end, err := parseHHMM(in.MonitoringEnd)
	if err != nil {
		return nil, badRequest("invalid_window", "monitoring_end must be HH:MM")
	}
	if start == end {
		return nil, badRequest("invalid_window", "zero-length monitoring windows are not allowed")
	}
	if end > 1440 {
		return nil, badRequest("invalid_window", "monitoring_end must be HH:MM (or 24:00 for end of day)")
	}
	if len(in.ExcludedApps) == 0 {
		return nil, badRequest("invalid_excluded_apps", "at least one excluded-app pattern is required")
	}
	for _, p := range in.ExcludedApps {
		if p == "" {
			return nil, badRequest("invalid_excluded_apps", "empty pattern")
		}
		if vErr := pattern.Validate(p); vErr != nil {
			return nil, badRequest("invalid_pattern", p+": "+vErr.Error())
		}
	}

	tx, err := s.DB.BeginTxx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback() //nolint:errcheck

	for _, id := range in.ExemptDepartments {
		var exists bool
		if err := tx.GetContext(ctx, &exists,
			`select exists(select 1 from departments where id=$1)`, id); err != nil {
			return nil, err
		}
		if !exists {
			return nil, badRequest("unknown_department", fmt.Sprintf("department %d does not exist", id))
		}
	}

	var version int32
	if err := tx.QueryRowxContext(ctx, `
		insert into policy_versions
			(monitoring_start_minute, monitoring_end_minute, published_by, note)
		values ($1,$2,$3,$4)
		returning version`, start, end, in.PublishedBy, in.Note).Scan(&version); err != nil {
		return nil, err
	}
	for i, p := range in.ExcludedApps {
		if _, err := tx.ExecContext(ctx, `
			insert into policy_excluded_apps(policy_version, position, pattern)
			values ($1,$2,$3)`, version, i, p); err != nil {
			return nil, err
		}
	}
	for _, id := range in.ExemptDepartments {
		if _, err := tx.ExecContext(ctx, `
			insert into policy_exempt_departments(policy_version, department_id)
			values ($1,$2)`, version, id); err != nil {
			return nil, err
		}
	}
	var publishedAt time.Time
	if err := tx.QueryRowxContext(ctx,
		`select published_at from policy_versions where version=$1`, version).Scan(&publishedAt); err != nil {
		return nil, err
	}
	if err := tx.Commit(); err != nil {
		return nil, err
	}
	return &VersionOut{Version: version, PublishedAt: publishedAt.UTC().Format(time.RFC3339)}, nil
}

// ---------------------------------------------------------------------------
// Classification publishing
// ---------------------------------------------------------------------------

type RuleInput struct {
	RuleID   string `json:"rule_id"`
	Pattern  string `json:"pattern"`
	Category string `json:"category"`
	Priority int    `json:"priority"`
}

type ClassificationInput struct {
	Rules       []RuleInput `json:"rules"`
	Note        string      `json:"note"`
	PublishedBy string      `json:"published_by"`
}

type ClassificationVersionOut struct {
	Version     int64  `json:"version"`
	PublishedAt string `json:"published_at"`
}

// PublishClassification inserts a new immutable rule set.
func (s *Service) PublishClassification(ctx context.Context, in ClassificationInput) (*ClassificationVersionOut, error) {
	if len(in.Rules) == 0 {
		return nil, badRequest("invalid_rules", "at least one rule is required")
	}
	seenIDs := map[string]bool{}
	for _, r := range in.Rules {
		if r.RuleID == "" {
			return nil, badRequest("invalid_rules", "rule_id required")
		}
		if seenIDs[r.RuleID] {
			return nil, badRequest("invalid_rules", "duplicate rule_id "+r.RuleID)
		}
		seenIDs[r.RuleID] = true
		if r.Pattern == "" {
			return nil, badRequest("invalid_rules", r.RuleID+": pattern required")
		}
		if vErr := pattern.Validate(r.Pattern); vErr != nil {
			return nil, badRequest("invalid_pattern", r.RuleID+": "+vErr.Error())
		}
		if r.Category != model.CategoryProductive &&
			r.Category != model.CategoryUnproductive &&
			r.Category != model.CategoryNeutral {
			return nil, badRequest("invalid_category",
				r.RuleID+": category must be productive|unproductive|neutral")
		}
		if r.Priority < 0 {
			return nil, badRequest("invalid_priority", r.RuleID+": priority must be >= 0")
		}
	}

	tx, err := s.DB.BeginTxx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback() //nolint:errcheck

	var version int64
	if err := tx.QueryRowxContext(ctx, `
		insert into classification_versions(published_by, note)
		values ($1,$2) returning version`, in.PublishedBy, in.Note).Scan(&version); err != nil {
		return nil, err
	}
	for _, r := range in.Rules {
		if _, err := tx.ExecContext(ctx, `
			insert into classification_rules
				(classification_version, rule_id, pattern, category, priority)
			values ($1,$2,$3,$4,$5)`,
			version, r.RuleID, r.Pattern, r.Category, r.Priority); err != nil {
			return nil, err
		}
	}
	var publishedAt time.Time
	if err := tx.QueryRowxContext(ctx,
		`select published_at from classification_versions where version=$1`, version).Scan(&publishedAt); err != nil {
		return nil, err
	}
	if err := tx.Commit(); err != nil {
		return nil, err
	}
	return &ClassificationVersionOut{Version: version, PublishedAt: publishedAt.UTC().Format(time.RFC3339)}, nil
}

// ---------------------------------------------------------------------------
// Rebuild
// ---------------------------------------------------------------------------

type RebuildInput struct {
	StartDate string `json:"start_date"` // YYYY-MM-DD local
	EndDate   string `json:"end_date"`   // inclusive
}

type RebuildOut struct {
	StartDate     string `json:"start_date"`
	EndDate       string `json:"end_date"`
	DailyBuckets  int    `json:"daily_buckets_rebuilt"`
	WeeklyBuckets int    `json:"weekly_buckets_rebuilt"`
}

// Rebuild recomputes every daily and weekly summary bucket overlapping the date
// range, entirely from activity_snapshots. It refuses if the range touches a
// date whose raw data was purged: a partial-history rebuild must never replace
// complete summaries.
func (s *Service) Rebuild(ctx context.Context, in RebuildInput) (*RebuildOut, error) {
	start, err := time.Parse("2006-01-02", in.StartDate)
	if err != nil {
		return nil, badRequest("invalid_date", "start_date must be YYYY-MM-DD")
	}
	end, err := time.Parse("2006-01-02", in.EndDate)
	if err != nil {
		return nil, badRequest("invalid_date", "end_date must be YYYY-MM-DD")
	}
	if end.Before(start) {
		return nil, badRequest("invalid_range", "end_date before start_date")
	}

	tx, err := s.DB.BeginTxx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback() //nolint:errcheck

	// Shared lock blocks purges for the duration; concurrent ingests proceed.
	if _, err := tx.ExecContext(ctx,
		`select pg_advisory_xact_lock_shared($1)`, retentionLockKey); err != nil {
		return nil, err
	}

	// Collect affected employee/date buckets: existing daily rows in range plus
	// every raw employee-local date within the UTC envelope.
	type eb struct {
		EmpID  int64  `db:"emp_id"`
		DeptID int64  `db:"dept_id"`
		Date   string `db:"d"`
	}

	var existing []eb
	if err := tx.SelectContext(ctx, &existing, `
		select employee_id as emp_id,
		       (select department_id from employees e2 where e2.id = d.employee_id) as dept_id,
		       local_date::text as d
		from daily_employee_summary d
		where local_date between $1::date and $2::date`,
		in.StartDate, in.EndDate); err != nil {
		return nil, err
	}

	// UTC envelope: [start 00:00 UTC-14h, end+1 00:00 UTC+12h).
	envStart := start.Add(-14 * time.Hour)
	envEnd := end.AddDate(0, 0, 1).Add(12 * time.Hour)
	var rawDates []eb
	if err := tx.SelectContext(ctx, &rawDates, `
		select distinct s.employee_id as emp_id, e.department_id as dept_id,
		       ((s.bucket_time at time zone e.timezone)::date)::text as d
		from activity_snapshots s
		join employees e on e.id = s.employee_id
		where s.bucket_time >= $1 and s.bucket_time < $2
		  and ((s.bucket_time at time zone e.timezone)::date) between $3::date and $4::date`,
		envStart, envEnd, in.StartDate, in.EndDate); err != nil {
		return nil, err
	}

	bucketMap := map[aggregate.Bucket]bool{}
	addBucket := func(row eb) error {
		loc, lErr := time.LoadLocation(employeeZone(ctx, tx, row.EmpID))
		if lErr != nil {
			return lErr
		}
		d, _ := time.Parse("2006-01-02", row.Date)
		// Noon on that date IN the employee's zone is safely inside the local
		// day for every IANA offset, so WeekStart sees the right local date.
		localNoon := time.Date(d.Year(), d.Month(), d.Day(), 12, 0, 0, 0, loc)
		bucketMap[aggregate.Bucket{
			EmployeeID:   row.EmpID,
			DepartmentID: row.DeptID,
			LocalDate:    row.Date,
			WeekStart:    timeutil.WeekStart(localNoon, loc),
		}] = true
		return nil
	}
	for _, row := range existing {
		if err := addBucket(row); err != nil {
			return nil, err
		}
	}
	for _, row := range rawDates {
		if err := addBucket(row); err != nil {
			return nil, err
		}
	}

	// Refuse before touching anything if any targeted bucket is frozen. Frozen
	// buckets overlap purged raw history; recomputing them from surviving rows
	// would overwrite a complete statistic with partial data.
	dayKeys := map[[2]string]bool{}
	weekKeys := map[[2]string]bool{}
	for b := range bucketMap {
		dayKeys[[2]string{fmt.Sprint(b.EmployeeID), b.LocalDate}] = true
		weekKeys[[2]string{fmt.Sprint(b.DepartmentID), b.WeekStart}] = true
	}
	for k := range dayKeys {
		var frozen bool
		if err := tx.GetContext(ctx, &frozen,
			`select coalesce((select frozen from daily_employee_summary
			                  where employee_id=$1::bigint and local_date=$2::date), false)`,
			k[0], k[1]); err != nil {
			return nil, err
		}
		if frozen {
			return nil, &APIError{http.StatusUnprocessableEntity, "rebuild_range_purged",
				"daily summary for employee " + k[0] + " on " + k[1] +
					" is frozen: raw history has been purged and complete statistics cannot be rebuilt"}
		}
	}
	for k := range weekKeys {
		var frozen bool
		if err := tx.GetContext(ctx, &frozen,
			`select coalesce((select frozen from weekly_department_summary
			                  where department_id=$1::bigint and week_start=$2::date), false)`,
			k[0], k[1]); err != nil {
			return nil, err
		}
		if frozen {
			return nil, &APIError{http.StatusUnprocessableEntity, "rebuild_range_purged",
				"weekly summary for department " + k[0] + " week of " + k[1] +
					" is frozen: raw history has been purged and complete statistics cannot be rebuilt"}
		}
	}

	var buckets []aggregate.Bucket
	for b := range bucketMap {
		buckets = append(buckets, b)
	}
	if err := aggregate.RecomputeAffected(ctx, tx, buckets); err != nil {
		return nil, err
	}
	if err := tx.Commit(); err != nil {
		return nil, err
	}
	weeks := map[string]bool{}
	days := map[string]bool{}
	for _, b := range buckets {
		weeks[fmt.Sprintf("%d|%s", b.DepartmentID, b.WeekStart)] = true
		days[fmt.Sprintf("%d|%s", b.EmployeeID, b.LocalDate)] = true
	}
	return &RebuildOut{
		StartDate:     in.StartDate,
		EndDate:       in.EndDate,
		DailyBuckets:  len(days),
		WeeklyBuckets: len(weeks),
	}, nil
}

func employeeZone(ctx context.Context, tx *sqlx.Tx, empID int64) string {
	var tz string
	if err := tx.GetContext(ctx, &tz, `select timezone from employees where id=$1`, empID); err != nil {
		return "UTC"
	}
	return tz
}

// ---------------------------------------------------------------------------
// Retention / purge
// ---------------------------------------------------------------------------

type PurgeInput struct {
	Cutoff      string `json:"cutoff"` // RFC3339 UTC; delete raw buckets < cutoff
	TriggeredBy string `json:"triggered_by"`
}

type PurgeOut struct {
	Cutoff           string `json:"cutoff"`
	DeletedRows      int64  `json:"deleted_raw_rows"`
	EarliestSnapshot string `json:"earliest_snapshot,omitempty"`
}

// Purge deletes raw snapshots older than cutoff, keeps summaries intact, and
// advances the retention marker so later rebuilds cannot overwrite complete
// summaries with partial (purged) history.
func (s *Service) Purge(ctx context.Context, in PurgeInput) (*PurgeOut, error) {
	cutoff, err := time.Parse(time.RFC3339, in.Cutoff)
	if err != nil {
		return nil, badRequest("invalid_cutoff", "cutoff must be RFC3339 UTC")
	}
	cutoff = cutoff.UTC()

	tx, err := s.DB.BeginTxx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback() //nolint:errcheck

	// Exclusive: blocks concurrent ingest and rebuild until the marker moves.
	if _, err := tx.ExecContext(ctx,
		`select pg_advisory_xact_lock($1)`, retentionLockKey); err != nil {
		return nil, err
	}

	var old sql.NullTime
	if err := tx.GetContext(ctx, &old,
		`select earliest_snapshot from retention_state where id=1`); err != nil && !errors.Is(err, sql.ErrNoRows) {
		return nil, err
	}
	if old.Valid && cutoff.Before(old.Time) {
		return nil, badRequest("cutoff_before_boundary",
			"cutoff precedes the existing retention boundary; the boundary can only move forward")
	}

	// --- Freeze summaries that can no longer be fully rebuilt ---------------
	// A local date is frozen whenever the UTC start of that local day precedes
	// the cutoff: deleted buckets (bucket_time < cutoff) may then fall inside
	// the day's UTC interval, so rebuilding it from surviving rows could lose
	// data. Days whose UTC interval starts at/after the cutoff are unaffected.
	// (timestamp AT TIME ZONE zone converts a local wall-clock value to UTC.)
	if _, err := tx.ExecContext(ctx, `
		update daily_employee_summary d
		set frozen = true
		from employees e
		where d.employee_id = e.id
		  and (d.local_date::timestamp at time zone e.timezone) < $1::timestamptz`,
		cutoff); err != nil {
		return nil, fmt.Errorf("freeze daily summaries: %w", err)
	}

	// Freeze a weekly row when any daily row in that department/local week is
	// frozen, or when the week contained raw rows about to be purged (handles
	// weeks whose daily summary no longer exists).
	if _, err := tx.ExecContext(ctx, `
		update weekly_department_summary w
		set frozen = true
		where exists (
		        select 1 from daily_employee_summary d
		        join employees e on e.id = d.employee_id
		        where e.department_id = w.department_id
		          and d.local_date between w.week_start and w.week_start + 6
		          and d.frozen
		      )
		   or exists (
		        select 1 from activity_snapshots s
		        join employees e on e.id = s.employee_id
		        where e.department_id = w.department_id
		          and s.bucket_time < $1
		          and date_trunc('week', (s.bucket_time at time zone e.timezone)::date)::date
		              = w.week_start
		      )`, cutoff); err != nil {
		return nil, fmt.Errorf("freeze weekly summaries: %w", err)
	}

	res, err := tx.ExecContext(ctx,
		`delete from activity_snapshots where bucket_time < $1`, cutoff)
	if err != nil {
		return nil, err
	}
	deleted, _ := res.RowsAffected()

	if _, err := tx.ExecContext(ctx,
		`insert into cleanup_events(cutoff, deleted_rows, triggered_by) values ($1,$2,$3)`,
		cutoff, deleted, in.TriggeredBy); err != nil {
		return nil, err
	}

	var earliest sql.NullTime
	if err := tx.GetContext(ctx, &earliest,
		`select min(bucket_time) from activity_snapshots`); err != nil {
		return nil, err
	}
	out := &PurgeOut{Cutoff: cutoff.Format(time.RFC3339), DeletedRows: deleted}
	if earliest.Valid {
		if _, err := tx.ExecContext(ctx, `
			insert into retention_state(id, earliest_snapshot, updated_at)
			values (1, $1, now())
			on conflict (id) do update set earliest_snapshot = excluded.earliest_snapshot,
			                               updated_at = now()`,
			earliest.Time); err != nil {
			return nil, err
		}
		out.EarliestSnapshot = earliest.Time.UTC().Format(time.RFC3339)
	} else {
		if _, err := tx.ExecContext(ctx, `delete from retention_state where id=1`); err != nil {
			return nil, err
		}
	}
	if err := tx.Commit(); err != nil {
		return nil, err
	}
	return out, nil
}

// RetentionStatus reports the rebuild boundary and cleanup history.
type RetentionStatus struct {
	EarliestSnapshot string         `json:"earliest_snapshot,omitempty"`
	HasPurged        bool           `json:"has_purged"`
	Events           []CleanupEvent `json:"cleanup_events"`
}

type CleanupEvent struct {
	ID          int64  `json:"id" db:"id"`
	Cutoff      string `json:"cutoff" db:"cutoff"`
	DeletedRows int64  `json:"deleted_rows" db:"deleted_rows"`
	TriggeredBy string `json:"triggered_by" db:"triggered_by"`
	RanAt       string `json:"ran_at" db:"ran_at"`
}

func (s *Service) RetentionStatus(ctx context.Context) (*RetentionStatus, error) {
	out := &RetentionStatus{}
	var earliest sql.NullTime
	if err := s.DB.GetContext(ctx, &earliest,
		`select earliest_snapshot from retention_state where id=1`); err != nil && !errors.Is(err, sql.ErrNoRows) {
		return nil, err
	}
	if earliest.Valid {
		out.HasPurged = true
		out.EarliestSnapshot = earliest.Time.UTC().Format(time.RFC3339)
	}
	rows, err := s.DB.QueryxContext(ctx, `
		select id, cutoff, deleted_rows, triggered_by, ran_at
		from cleanup_events order by id desc limit 50`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var e CleanupEvent
		var cutoff, ranAt time.Time
		if err := rows.Scan(&e.ID, &cutoff, &e.DeletedRows, &e.TriggeredBy, &ranAt); err != nil {
			return nil, err
		}
		e.Cutoff, e.RanAt = cutoff.UTC().Format(time.RFC3339), ranAt.UTC().Format(time.RFC3339)
		out.Events = append(out.Events, e)
	}
	return out, rows.Err()
}

func parseHHMM(s string) (int, error) {
	if s == "24:00" {
		return 1440, nil
	}
	if len(s) != 5 || s[2] != ':' {
		return 0, fmt.Errorf("bad HH:MM")
	}
	h := int(s[0]-'0')*10 + int(s[1]-'0')
	m := int(s[3]-'0')*10 + int(s[4]-'0')
	if h < 0 || h > 23 || m < 0 || m > 59 {
		return 0, fmt.Errorf("bad HH:MM")
	}
	return h*60 + m, nil
}
