// Package aggregate owns the only code path that writes daily and weekly
// summaries. Incremental ingest and full rebuilds call the same functions, so
// rebuilding from raw rows always reproduces exactly what incremental
// processing produced.
//
// All recomputation is "delete the bucket then insert the recomputed bucket"
// inside the caller's transaction, under deterministic advisory locks. Taking
// the same locks in the same (employee,date)/(department,week) order makes a
// rebuild concurrent with new ingests safe: they serialize per bucket and
// neither can lose nor double-count rows.
package aggregate

import (
	"context"
	"fmt"
	"hash/fnv"
	"sort"
	"time"

	"github.com/jmoiron/sqlx"
)

// A Bucket names one summary bucket that must be refreshed.
type Bucket struct {
	EmployeeID   int64
	DepartmentID int64
	LocalDate    string // YYYY-MM-DD, employee-local
	WeekStart    string // Monday of the local week
}

const (
	// Advisory lock key spaces: (kind 1 = employee/day, kind 2 = dept/week,
	// kind 3 = workstation/minute idempotency key).
	lockKindDay    = 1
	lockKindWeek   = 2
	lockKindMinute = 3

	// UTC scan window for a local calendar week: every IANA offset lies in
	// [-12h,+14h], so Monday 00:00 UTC-12 .. Sunday 24:00 UTC+14.
	weekScanBefore = 12 * time.Hour
	weekScanAfter  = 38 * time.Hour
)

// MinuteKey identifies one (workstation, minute) idempotency key.
type MinuteKey struct {
	WorkstationID string
	Bucket        time.Time
}

// LockMinuteKeys serializes transactions touching the same workstation/minute
// so that concurrent batches cannot race the duplicate/conflict check. Keys
// are acquired in a deterministic order to avoid deadlocks across batches.
func LockMinuteKeys(ctx context.Context, tx *sqlx.Tx, keys []MinuteKey) error {
	hashed := make([]int64, 0, len(keys))
	seen := map[int64]bool{}
	for _, k := range keys {
		h := fnv.New64a()
		_, _ = h.Write([]byte(k.WorkstationID))
		wsID := int64(h.Sum64())
		minute := k.Bucket.Unix() / 60
		key := hashLockKey(lockKindMinute, wsID, minute)
		if !seen[key] {
			seen[key] = true
			hashed = append(hashed, key)
		}
	}
	sort.Slice(hashed, func(i, j int) bool { return hashed[i] < hashed[j] })
	for _, key := range hashed {
		if _, err := tx.ExecContext(ctx, `select pg_advisory_xact_lock($1)`, key); err != nil {
			return fmt.Errorf("lock minute key: %w", err)
		}
	}
	return nil
}

// RecomputeAffected refreshes every distinct daily and weekly bucket listed.
// Buckets are locked in a globally consistent order to avoid deadlocks.
func RecomputeAffected(ctx context.Context, tx *sqlx.Tx, buckets []Bucket) error {
	dayKeys := map[[2]int64]bool{}
	weekKeys := map[[2]int64]bool{}
	for _, b := range buckets {
		dk := [2]int64{b.EmployeeID, dateOrdinal(b.LocalDate)}
		wk := [2]int64{b.DepartmentID, dateOrdinal(b.WeekStart)}
		dayKeys[dk] = true
		weekKeys[wk] = true
	}

	var dayList, weekList [][2]int64
	for k := range dayKeys {
		dayList = append(dayList, k)
	}
	for k := range weekKeys {
		weekList = append(weekList, k)
	}
	// Days first, then weeks; within each kind ascending by both keys. This
	// matches the order every other caller uses (ingest, rebuild, purge).
	sort.Slice(dayList, func(i, j int) bool {
		if dayList[i][0] != dayList[j][0] {
			return dayList[i][0] < dayList[j][0]
		}
		return dayList[i][1] < dayList[j][1]
	})
	sort.Slice(weekList, func(i, j int) bool {
		if weekList[i][0] != weekList[j][0] {
			return weekList[i][0] < weekList[j][0]
		}
		return weekList[i][1] < weekList[j][1]
	})

	for _, k := range dayList {
		if _, err := tx.ExecContext(ctx,
			`select pg_advisory_xact_lock($1)`, hashLockKey(lockKindDay, k[0], k[1])); err != nil {
			return fmt.Errorf("lock day bucket: %w", err)
		}
	}
	for _, k := range weekList {
		if _, err := tx.ExecContext(ctx,
			`select pg_advisory_xact_lock($1)`, hashLockKey(lockKindWeek, k[0], k[1])); err != nil {
			return fmt.Errorf("lock week bucket: %w", err)
		}
	}

	for _, k := range dayList {
		empID, ord := k[0], k[1]
		frozen, err := isDayFrozen(ctx, tx, empID, ordinalDate(ord))
		if err != nil {
			return err
		}
		if frozen {
			// Purged history: keep the stored complete statistic untouched.
			continue
		}
		if err := recomputeDayLocked(ctx, tx, empID, ordinalDate(ord)); err != nil {
			return err
		}
	}
	for _, k := range weekList {
		deptID, ord := k[0], k[1]
		frozen, err := isWeekFrozen(ctx, tx, deptID, ordinalDate(ord))
		if err != nil {
			return err
		}
		if frozen {
			continue
		}
		if err := recomputeWeekLocked(ctx, tx, deptID, ordinalDate(ord)); err != nil {
			return err
		}
	}
	return nil
}

func isDayFrozen(ctx context.Context, tx *sqlx.Tx, employeeID int64, date string) (bool, error) {
	var frozen bool
	err := tx.GetContext(ctx, &frozen,
		`select coalesce((select frozen from daily_employee_summary
		                 where employee_id=$1 and local_date=$2::date), false)`,
		employeeID, date)
	if err != nil {
		return false, fmt.Errorf("check day frozen: %w", err)
	}
	return frozen, nil
}

func isWeekFrozen(ctx context.Context, tx *sqlx.Tx, departmentID int64, weekStart string) (bool, error) {
	var frozen bool
	err := tx.GetContext(ctx, &frozen,
		`select coalesce((select frozen from weekly_department_summary
		                 where department_id=$1 and week_start=$2::date), false)`,
		departmentID, weekStart)
	if err != nil {
		return false, fmt.Errorf("check week frozen: %w", err)
	}
	return frozen, nil
}

// recomputeDayLocked rebuilds one employee/local-date row from raw snapshots.
// The UTC range is derived exactly from that employee's zone for that date,
// so it always matches the buckets used at ingest.
func recomputeDayLocked(ctx context.Context, tx *sqlx.Tx, employeeID int64, date string) error {
	var tz string
	if err := tx.GetContext(ctx, &tz,
		`select timezone from employees where id = $1`, employeeID); err != nil {
		return fmt.Errorf("load employee timezone: %w", err)
	}
	loc, err := time.LoadLocation(tz)
	if err != nil {
		return fmt.Errorf("load timezone %q: %w", tz, err)
	}

	start, end := dayUTCRange(date, loc)

	if _, err := tx.ExecContext(ctx,
		`delete from daily_employee_summary where employee_id = $1 and local_date = $2::date`,
		employeeID, date); err != nil {
		return err
	}

	if _, err := tx.ExecContext(ctx, `
		insert into daily_employee_summary (
			employee_id, local_date,
			productive_count, unproductive_count, neutral_count, total_count,
			policy_version_min, policy_version_max,
			classification_version_min, classification_version_max)
		select $1, $2::date,
			coalesce(sum(activity_count) filter (where category = 'productive'), 0),
			coalesce(sum(activity_count) filter (where category = 'unproductive'), 0),
			coalesce(sum(activity_count) filter (where category = 'neutral'), 0),
			coalesce(sum(activity_count), 0),
			coalesce(min(policy_version), 0), coalesce(max(policy_version), 0),
			coalesce(min(classification_version), 0), coalesce(max(classification_version), 0)
		from activity_snapshots
		where employee_id = $1
		  and bucket_time >= $3 and bucket_time < $4
		having count(*) > 0`,
		employeeID, date, start, end); err != nil {
		return fmt.Errorf("recompute daily summary: %w", err)
	}
	return nil
}

// recomputeWeekLocked rebuilds one department/local-week row. The UTC scan
// range covers all possible IANA offsets; the query groups each raw row by the
// employee's own zone and keeps only rows whose local week equals weekStart, so
// no rows from adjacent weeks leak in.
func recomputeWeekLocked(ctx context.Context, tx *sqlx.Tx, departmentID int64, weekStart string) error {
	start, err := time.Parse("2006-01-02", weekStart)
	if err != nil {
		return fmt.Errorf("invalid week start: %w", err)
	}
	utcStart := start.Add(-weekScanBefore)
	utcEnd := start.AddDate(0, 0, 7).Add(weekScanAfter)

	if _, err := tx.ExecContext(ctx,
		`delete from weekly_department_summary where department_id = $1 and week_start = $2::date`,
		departmentID, weekStart); err != nil {
		return err
	}

	if _, err := tx.ExecContext(ctx, `
		insert into weekly_department_summary (
			department_id, week_start,
			productive_count, unproductive_count, neutral_count, total_count)
		select $1, $2::date,
			coalesce(sum(s.activity_count) filter (where s.category = 'productive'), 0),
			coalesce(sum(s.activity_count) filter (where s.category = 'unproductive'), 0),
			coalesce(sum(s.activity_count) filter (where s.category = 'neutral'), 0),
			coalesce(sum(s.activity_count), 0)
		from activity_snapshots s
		join employees e on e.id = s.employee_id
		where e.department_id = $1
		  and s.bucket_time >= $3 and s.bucket_time < $4
		  and (date_trunc('week', (s.bucket_time at time zone e.timezone)::date)::date) = $2::date
		having count(*) > 0`,
		departmentID, weekStart, utcStart, utcEnd); err != nil {
		return fmt.Errorf("recompute weekly summary: %w", err)
	}
	return nil
}

// dayUTCRange returns the half-open UTC interval of one local date in loc.
func dayUTCRange(date string, loc *time.Location) (time.Time, time.Time) {
	d, err := time.Parse("2006-01-02", date)
	if err != nil {
		panic("invalid date " + date)
	}
	startLocal := time.Date(d.Year(), d.Month(), d.Day(), 0, 0, 0, 0, loc)
	return startLocal.UTC(), startLocal.AddDate(0, 0, 1).UTC()
}

// dateOrdinal turns "YYYY-MM-DD" into a comparable integer (days since 0000).
func dateOrdinal(date string) int64 {
	t, err := time.Parse("2006-01-02", date)
	if err != nil {
		panic("invalid date " + date)
	}
	return t.Unix() / 86400
}

func ordinalDate(ord int64) string {
	return time.Unix(ord*86400, 0).UTC().Format("2006-01-02")
}

// hashLockKey folds the lock kind and two int64 ids into the single int64 key
// used by the one-argument advisory-lock form. Collisions are harmless (they
// just serialize unrelated buckets a bit more); FNV keeps them rare.
func hashLockKey(kind, a, b int64) int64 {
	const (
		offset64 = 1469598103934665603
		prime64  = 1099511628211
		mask64   = 1<<64 - 1
	)
	var h uint64 = offset64
	write := func(x int64) {
		for i := 0; i < 8; i++ {
			h ^= uint64(x>>(i*8)) & 0xff
			h *= prime64
			h &= mask64
		}
	}
	write(kind)
	write(a)
	write(b)
	return int64(h)
}
