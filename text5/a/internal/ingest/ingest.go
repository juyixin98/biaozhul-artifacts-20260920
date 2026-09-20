// Package ingest receives per-minute activity snapshots from monitoring
// agents. It enforces batch validation, idempotency on (workstation, minute),
// privacy filtering (monitoring window, excluded apps, exempt departments)
// and stamps every stored row with the policy and classification-rule
// versions that were actually applied.
package ingest

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/jmoiron/sqlx"

	"desklens/internal/aggregate"
	"desklens/internal/classify"
)

const MaxBatchSize = 5000

type Snapshot struct {
	WorkstationID string    `json:"workstation_id"`
	EmployeeID    int64     `json:"employee_id"`
	Timestamp     time.Time `json:"timestamp"` // UTC; truncated to the minute
	AppName       string    `json:"app_name"`
	ActivityCount int       `json:"activity_count"`
}

type FilteredItem struct {
	Index  int    `json:"index"`
	Reason string `json:"reason"` // department_exempt | app_excluded | outside_monitoring_window
}

type Result struct {
	Accepted      int            `json:"accepted"`
	Duplicates    int            `json:"duplicates"`
	Filtered      []FilteredItem `json:"filtered"`
	PolicyVersion int            `json:"policy_version"`
	RulesVersion  int            `json:"rules_version"`
}

// ValidationError rejects the whole batch with HTTP 400; nothing is stored.
type ValidationError struct{ Problems []string }

func (e *ValidationError) Error() string { return strings.Join(e.Problems, "; ") }

// ConflictError rejects the whole batch with HTTP 409: a snapshot with the
// same idempotency key but different content already exists.
type ConflictError struct {
	Index         int
	WorkstationID string
	Minute        time.Time
}

func (e *ConflictError) Error() string {
	return fmt.Sprintf("snapshot %d conflicts with existing record for workstation %s at %s",
		e.Index, e.WorkstationID, e.Minute.Format(time.RFC3339))
}

type policy struct {
	Version      int
	WorkStartMin int // minutes since local midnight, inclusive
	WorkEndMin   int // exclusive
	ExcludedApps []string
}

type employeeInfo struct {
	ID           int64
	DepartmentID int64
	Timezone     *time.Location
	Exempt       bool
}

// Process validates and stores a batch of snapshots atomically, then
// recomputes the affected daily/weekly summaries in the same transaction.
func Process(ctx context.Context, db *sqlx.DB, snaps []Snapshot) (*Result, error) {
	if len(snaps) == 0 {
		return nil, &ValidationError{Problems: []string{"empty batch"}}
	}
	if len(snaps) > MaxBatchSize {
		return nil, &ValidationError{Problems: []string{fmt.Sprintf("batch too large: %d > %d", len(snaps), MaxBatchSize)}}
	}

	// --- validation pass (read-only; any problem fails the whole batch) ---
	var problems []string
	wsCache := map[string]int64{}
	empCache := map[int64]*employeeInfo{}

	lookupWorkstation := func(id string) (int64, bool) {
		if empID, ok := wsCache[id]; ok {
			return empID, true
		}
		var empID int64
		err := db.GetContext(ctx, &empID,
			`SELECT employee_id FROM workstations WHERE id = $1`, id)
		if err != nil {
			return 0, false
		}
		wsCache[id] = empID
		return empID, true
	}
	lookupEmployee := func(id int64) (*employeeInfo, bool) {
		if e, ok := empCache[id]; ok {
			return e, true
		}
		var row struct {
			DepartmentID int64  `db:"department_id"`
			Timezone     string `db:"timezone"`
			Exempt       bool   `db:"exempt"`
		}
		err := db.GetContext(ctx, &row, `
			SELECT e.department_id, e.timezone, d.exempt
			FROM employees e JOIN departments d ON d.id = e.department_id
			WHERE e.id = $1`, id)
		if err != nil {
			return nil, false
		}
		loc, err := time.LoadLocation(row.Timezone)
		if err != nil {
			loc = time.UTC
		}
		e := &employeeInfo{ID: id, DepartmentID: row.DepartmentID, Timezone: loc, Exempt: row.Exempt}
		empCache[id] = e
		return e, true
	}

	for i, s := range snaps {
		switch {
		case s.WorkstationID == "":
			problems = append(problems, fmt.Sprintf("snapshot %d: workstation_id is required", i))
		case s.Timestamp.IsZero():
			problems = append(problems, fmt.Sprintf("snapshot %d: timestamp is required", i))
		case strings.TrimSpace(s.AppName) == "":
			problems = append(problems, fmt.Sprintf("snapshot %d: app_name is required", i))
		case s.ActivityCount < 0:
			problems = append(problems, fmt.Sprintf("snapshot %d: activity_count must be >= 0", i))
		}
		if len(problems) > 0 {
			continue
		}
		wsEmp, ok := lookupWorkstation(s.WorkstationID)
		if !ok {
			problems = append(problems, fmt.Sprintf("snapshot %d: unknown workstation %q", i, s.WorkstationID))
			continue
		}
		if wsEmp != s.EmployeeID {
			problems = append(problems, fmt.Sprintf(
				"snapshot %d: employee_id %d does not match workstation %s (owned by %d)",
				i, s.EmployeeID, s.WorkstationID, wsEmp))
			continue
		}
		if _, ok := lookupEmployee(s.EmployeeID); !ok {
			problems = append(problems, fmt.Sprintf("snapshot %d: unknown employee %d", i, s.EmployeeID))
		}
	}
	if len(problems) > 0 {
		return nil, &ValidationError{Problems: problems}
	}

	// --- load current policy and rule versions ---
	pol, err := currentPolicy(ctx, db)
	if err != nil {
		return nil, err
	}
	rules, rulesVersion, err := currentRules(ctx, db)
	if err != nil {
		return nil, err
	}

	res := &Result{Filtered: []FilteredItem{}, PolicyVersion: pol.Version, RulesVersion: rulesVersion}

	tx, err := db.BeginTxx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()

	type empDay struct {
		emp  int64
		day  time.Time
		dept int64
	}
	affected := map[empDay]struct{}{}

	for i, s := range snaps {
		emp := empCache[s.EmployeeID]
		minute := s.Timestamp.UTC().Truncate(time.Minute)

		// Privacy filtering: excluded data never enters the raw table.
		switch {
		case emp.Exempt:
			res.Filtered = append(res.Filtered, FilteredItem{i, "department_exempt"})
			continue
		case appExcluded(pol.ExcludedApps, s.AppName):
			res.Filtered = append(res.Filtered, FilteredItem{i, "app_excluded"})
			continue
		}
		local := minute.In(emp.Timezone)
		tod := local.Hour()*60 + local.Minute()
		if tod < pol.WorkStartMin || tod >= pol.WorkEndMin {
			res.Filtered = append(res.Filtered, FilteredItem{i, "outside_monitoring_window"})
			continue
		}

		localDate := time.Date(local.Year(), local.Month(), local.Day(), 0, 0, 0, 0, time.UTC)
		category := classify.Match(rules, s.AppName)
		hash := contentHash(s.EmployeeID, minute, s.AppName, s.ActivityCount)

		var insertedID int64
		err := tx.QueryRowContext(ctx, `
			INSERT INTO raw_snapshots
				(workstation_id, employee_id, minute_utc, app_name, activity_count,
				 local_date, category, policy_version, rules_version, content_hash)
			VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
			ON CONFLICT (workstation_id, minute_utc) DO NOTHING
			RETURNING id`,
			s.WorkstationID, s.EmployeeID, minute, s.AppName, s.ActivityCount,
			localDate.Format("2006-01-02"), string(category), pol.Version, rulesVersion, hash,
		).Scan(&insertedID)
		switch {
		case err == nil:
			res.Accepted++
			affected[empDay{emp.ID, localDate, emp.DepartmentID}] = struct{}{}
		case errors.Is(err, sql.ErrNoRows):
			// Idempotency key already present: identical content is a
			// duplicate (processed once), different content is a conflict.
			var existingHash []byte
			if qerr := tx.GetContext(ctx, &existingHash, `
				SELECT content_hash FROM raw_snapshots
				WHERE workstation_id = $1 AND minute_utc = $2`,
				s.WorkstationID, minute); qerr != nil {
				return nil, qerr
			}
			if string(existingHash) == string(hash) {
				res.Duplicates++
				continue
			}
			return nil, &ConflictError{Index: i, WorkstationID: s.WorkstationID, Minute: minute}
		default:
			return nil, err
		}
	}

	// Recompute only the affected employee-days and department-weeks; late
	// snapshots therefore touch exactly the dates they belong to.
	for ed := range affected {
		if err := aggregate.RecomputeDailyTx(ctx, tx, ed.emp, ed.day); err != nil {
			return nil, err
		}
	}
	weeks := map[string]struct {
		dept int64
		week time.Time
	}{}
	for ed := range affected {
		w := aggregate.WeekStart(ed.day)
		weeks[fmt.Sprintf("%d|%s", ed.dept, w.Format("2006-01-02"))] = struct {
			dept int64
			week time.Time
		}{ed.dept, w}
	}
	for _, dw := range weeks {
		if err := aggregate.RecomputeWeeklyTx(ctx, tx, dw.dept, dw.week); err != nil {
			return nil, err
		}
	}

	if err := tx.Commit(); err != nil {
		return nil, err
	}
	return res, nil
}

func contentHash(employeeID int64, minute time.Time, app string, count int) []byte {
	sum := sha256.Sum256([]byte(fmt.Sprintf("%d|%s|%s|%d",
		employeeID, minute.UTC().Format(time.RFC3339), app, count)))
	return sum[:]
}

func appExcluded(patterns []string, app string) bool {
	for _, p := range patterns {
		if classify.GlobMatch(p, app) {
			return true
		}
	}
	return false
}

func currentPolicy(ctx context.Context, db *sqlx.DB) (*policy, error) {
	var row struct {
		Version   int    `db:"version"`
		WorkStart string `db:"work_start"`
		WorkEnd   string `db:"work_end"`
		Excluded  string `db:"excluded_apps"`
	}
	err := db.GetContext(ctx, &row, `
		SELECT version, to_char(work_start, 'HH24:MI') AS work_start,
		       to_char(work_end, 'HH24:MI') AS work_end,
		       excluded_apps::text AS excluded_apps
		FROM policies ORDER BY version DESC LIMIT 1`)
	if err != nil {
		return nil, fmt.Errorf("no monitoring policy published: %w", err)
	}
	p := &policy{Version: row.Version}
	var h, m int
	if _, err := fmt.Sscanf(row.WorkStart, "%d:%d", &h, &m); err != nil {
		return nil, fmt.Errorf("bad policy work_start %q: %w", row.WorkStart, err)
	}
	p.WorkStartMin = h*60 + m
	if _, err := fmt.Sscanf(row.WorkEnd, "%d:%d", &h, &m); err != nil {
		return nil, fmt.Errorf("bad policy work_end %q: %w", row.WorkEnd, err)
	}
	p.WorkEndMin = h*60 + m
	if err := json.Unmarshal([]byte(row.Excluded), &p.ExcludedApps); err != nil {
		return nil, fmt.Errorf("bad policy excluded_apps: %w", err)
	}
	return p, nil
}

func currentRules(ctx context.Context, db *sqlx.DB) ([]classify.Rule, int, error) {
	var rows []struct {
		ID       int64  `db:"id"`
		Pattern  string `db:"pattern"`
		Category string `db:"category"`
		Priority int    `db:"priority"`
	}
	var version int
	if err := db.GetContext(ctx, &version,
		`SELECT COALESCE(MAX(version), 0) FROM classification_rules`); err != nil {
		return nil, 0, err
	}
	if version == 0 {
		return nil, 0, errors.New("no classification rules published")
	}
	if err := db.SelectContext(ctx, &rows, `
		SELECT id, pattern, category, priority
		FROM classification_rules WHERE version = $1`, version); err != nil {
		return nil, 0, err
	}
	rules := make([]classify.Rule, len(rows))
	for i, r := range rows {
		rules[i] = classify.Rule{
			ID:       r.ID,
			Pattern:  r.Pattern,
			Category: classify.Category(r.Category),
			Priority: r.Priority,
		}
	}
	return rules, version, nil
}
