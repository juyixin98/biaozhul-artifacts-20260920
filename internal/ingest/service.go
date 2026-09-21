package ingest

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"time"

	"desklens/internal/classify"
	"desklens/internal/policy"
	"desklens/internal/repo"
	"desklens/internal/timeutil"
)

// MaxBatchSize caps one POST; oversized batches are a validation failure.
const MaxBatchSize = 1000

// Outcome counts what happened to a batch. Filtered rows are acknowledged and
// discarded — they never reach storage.
type Outcome struct {
	Received  int            `json:"received"`
	Accepted  int            `json:"accepted"`
	Duplicate int            `json:"duplicate"`
	Filtered  FilteredCounts `json:"filtered"`
	// Recomputed lists the summary keys touched so callers/tests can observe
	// the incremental effect.
	RecomputedDays  int `json:"recomputed_days"`
	RecomputedWeeks int `json:"recomputed_weeks"`
	PolicyVersion   int `json:"policy_version"`
	ClassifVersion  int `json:"classif_version"`
}

type FilteredCounts struct {
	ExemptDepartment int `json:"exempt_department"`
	ExcludedApp      int `json:"excluded_app"`
	OutsideWindow    int `json:"outside_window"`
}

// ValidationError describes a structural problem with one batch item.
type ValidationError struct {
	Index  int    `json:"index"`
	Reason string `json:"reason"`
}

type BatchError struct {
	Status int
	Code   string
	Items  []ValidationError
}

func (e *BatchError) Error() string {
	if len(e.Items) > 0 {
		return fmt.Sprintf("ingest: %s (%d item errors)", e.Code, len(e.Items))
	}
	return "ingest: " + e.Code
}

func validationError(items []ValidationError) *BatchError {
	return &BatchError{Status: 400, Code: "validation_failed", Items: items}
}

type Service struct {
	repo *repo.Repo
}

func NewService(r *repo.Repo) *Service { return &Service{repo: r} }

// candidate is a validated, policy-resolved snapshot ready to persist.
type candidate struct {
	row repo.RawSnapshot
}

// Ingest runs the whole batch in one transaction. Any validation failure,
// workstation/employee mismatch or conflicting idempotency key rolls back the
// entire batch: partial batches are never stored.
func (s *Service) Ingest(ctx context.Context, in []repo.SnapshotIn) (Outcome, error) {
	var out Outcome
	out.Received = len(in)
	if len(in) == 0 {
		return out, validationError(nil)
	}
	if len(in) > MaxBatchSize {
		return out, &BatchError{Status: 400, Code: "batch_too_large"}
	}

	// Pure structural validation up front.
	if verrs := validate(in); len(verrs) > 0 {
		return out, validationError(verrs)
	}

	tx, err := s.repo.BeginTxx(ctx)
	if err != nil {
		return out, err
	}
	defer tx.Rollback()

	pol, err := s.repo.CurrentPolicy(ctx, tx)
	if err != nil {
		return out, err
	}
	cls, err := s.repo.CurrentClassification(ctx, tx)
	if err != nil {
		return out, err
	}
	filter, err := policy.NewFilter(pol)
	if err != nil {
		return out, err
	}
	matcher, err := classify.NewMatcher(cls)
	if err != nil {
		return out, err
	}
	out.PolicyVersion = filter.Version()
	out.ClassifVersion = matcher.Version()

	empCache := map[int64]repo.Employee{}
	wsCache := map[int64]repo.Workstation{}
	locCache := map[string]*time.Location{}
	byKey := map[string]int{} // key -> index into cand (in-batch dedup)

	var cands []candidate
	addFiltered := func(reason string) {
		switch reason {
		case "exempt_department":
			out.Filtered.ExemptDepartment++
		case "excluded_app":
			out.Filtered.ExcludedApp++
		case "outside_window":
			out.Filtered.OutsideWindow++
		}
	}

	for i, item := range in {
		minute := timeutil.TruncateMinuteUTC(item.MinuteUTC)

		ws, ok := wsCache[item.WorkstationID]
		if !ok {
			ws, err = s.repo.Workstation(ctx, tx, item.WorkstationID)
			if err != nil {
				if errors.Is(err, repo.ErrNotFound) {
					return out, validationError([]ValidationError{{Index: i, Reason: "unknown_workstation"}})
				}
				return out, err
			}
			wsCache[ws.ID] = ws
		}
		emp, ok := empCache[item.EmployeeID]
		if !ok {
			emp, err = s.repo.Employee(ctx, tx, item.EmployeeID)
			if err != nil {
				if errors.Is(err, repo.ErrNotFound) {
					return out, validationError([]ValidationError{{Index: i, Reason: "unknown_employee"}})
				}
				return out, err
			}
			empCache[emp.ID] = emp
		}
		if ws.EmployeeID != emp.ID {
			return out, validationError([]ValidationError{{
				Index: i, Reason: "workstation_bound_to_other_employee",
			}})
		}
		if !emp.Active {
			return out, validationError([]ValidationError{{Index: i, Reason: "employee_inactive"}})
		}

		loc, ok := locCache[emp.Timezone]
		if !ok {
			loc, err = time.LoadLocation(emp.Timezone)
			if err != nil {
				return out, fmt.Errorf("employee %d timezone %q: %w", emp.ID, emp.Timezone, err)
			}
			locCache[emp.Timezone] = loc
		}

		// Privacy/scope filters, in fixed order. None of these rows is ever
		// written, so they can't reach raw storage or statistics.
		switch {
		case filter.ExemptDepartment(emp.DepartmentID):
			addFiltered("exempt_department")
			continue
		case filter.ExcludedApp(item.AppName):
			addFiltered("excluded_app")
			continue
		case !filter.InWindow(minute, loc):
			addFiltered("outside_window")
			continue
		}

		localDate := timeutil.LocalDate(minute, loc)
		row := repo.RawSnapshot{
			WorkstationID:  ws.ID,
			EmployeeID:     emp.ID,
			DepartmentID:   emp.DepartmentID,
			MinuteUTC:      minute,
			AppName:        item.AppName,
			ActivityCount:  item.ActivityCount,
			PolicyVersion:  filter.Version(),
			ClassifVersion: matcher.Version(),
			Category:       matcher.Match(item.AppName),
			LocalDate:      localDate,
		}
		key := idempotencyKey(row.WorkstationID, row.MinuteUTC, row.AppName)
		if prev, dup := byKey[key]; dup {
			// Same key twice inside one batch: identical content is collapsed
			// into one insert; different content rejects the whole batch.
			if cands[prev].row.ActivityCount != row.ActivityCount {
				return out, &BatchError{Status: 409, Code: "conflicting_idempotency_key"}
			}
			out.Duplicate++
			continue
		}
		byKey[key] = len(cands)
		cands = append(cands, candidate{row: row})
	}

	if len(cands) > 0 {
		rows := make([]repo.RawSnapshot, len(cands))
		for i, c := range cands {
			rows[i] = c.row
		}
		stored, err := s.repo.FindStored(ctx, tx, rows)
		if err != nil {
			return out, err
		}
		storedCount := map[string]int{}
		for _, sr := range stored {
			storedCount[idempotencyKey(sr.WorkstationID, sr.MinuteUTC, sr.AppName)] = sr.ActivityCount
		}
		// Deterministic order keeps lock acquisition deadlock-free even
		// within a batch.
		sort.Slice(rows, func(i, j int) bool {
			ki := idempotencyKey(rows[i].WorkstationID, rows[i].MinuteUTC, rows[i].AppName)
			kj := idempotencyKey(rows[j].WorkstationID, rows[j].MinuteUTC, rows[j].AppName)
			return ki < kj
		})

		type dayKey struct {
			emp int64
			day time.Time
		}
		type weekKey struct {
			dept int64
			week time.Time
		}
		days := map[dayKey]bool{}
		weeks := map[weekKey]bool{}

		for _, row := range rows {
			key := idempotencyKey(row.WorkstationID, row.MinuteUTC, row.AppName)
			if existing, isStored := storedCount[key]; isStored {
				// Repeat content is processed exactly once; conflicting
				// content is refused rather than overwriting.
				if existing != row.ActivityCount {
					return out, &BatchError{Status: 409, Code: "conflicting_idempotency_key"}
				}
				out.Duplicate++
				continue
			}
			if err := s.repo.InsertRaw(ctx, tx, row); err != nil {
				return out, err
			}
			out.Accepted++
			days[dayKey{row.EmployeeID, row.LocalDate}] = true
			weeks[weekKey{row.DepartmentID, timeutil.MondayOfWeek(row.LocalDate)}] = true
		}

		sortedDays := make([]dayKey, 0, len(days))
		for k := range days {
			sortedDays = append(sortedDays, k)
		}
		sort.Slice(sortedDays, func(i, j int) bool {
			if sortedDays[i].emp != sortedDays[j].emp {
				return sortedDays[i].emp < sortedDays[j].emp
			}
			return sortedDays[i].day.Before(sortedDays[j].day)
		})
		for _, k := range sortedDays {
			if err := repo.LockDaily(ctx, tx, k.emp, k.day); err != nil {
				return out, err
			}
		}
		for _, k := range sortedDays {
			if err := s.repo.RecomputeDaily(ctx, tx, k.emp, k.day); err != nil {
				return out, err
			}
		}
		out.RecomputedDays = len(sortedDays)

		sortedWeeks := make([]weekKey, 0, len(weeks))
		for k := range weeks {
			sortedWeeks = append(sortedWeeks, k)
		}
		sort.Slice(sortedWeeks, func(i, j int) bool {
			if sortedWeeks[i].dept != sortedWeeks[j].dept {
				return sortedWeeks[i].dept < sortedWeeks[j].dept
			}
			return sortedWeeks[i].week.Before(sortedWeeks[j].week)
		})
		for _, k := range sortedWeeks {
			if err := repo.LockWeekly(ctx, tx, k.dept, k.week); err != nil {
				return out, err
			}
		}
		for _, k := range sortedWeeks {
			if err := s.repo.RecomputeWeekly(ctx, tx, k.dept, k.week); err != nil {
				return out, err
			}
		}
		out.RecomputedWeeks = len(sortedWeeks)
	}

	if err := tx.Commit(); err != nil {
		return out, err
	}
	return out, nil
}

func validate(in []repo.SnapshotIn) []ValidationError {
	var errs []ValidationError
	for i, s := range in {
		if s.WorkstationID <= 0 {
			errs = append(errs, ValidationError{Index: i, Reason: "workstation_id_required"})
		}
		if s.EmployeeID <= 0 {
			errs = append(errs, ValidationError{Index: i, Reason: "employee_id_required"})
		}
		if s.AppName == "" {
			errs = append(errs, ValidationError{Index: i, Reason: "app_name_required"})
		}
		if len(s.AppName) > 256 {
			errs = append(errs, ValidationError{Index: i, Reason: "app_name_too_long"})
		}
		if s.ActivityCount < 0 {
			errs = append(errs, ValidationError{Index: i, Reason: "activity_count_negative"})
		}
		if s.MinuteUTC.IsZero() {
			errs = append(errs, ValidationError{Index: i, Reason: "minute_utc_required"})
		}
	}
	return errs
}

// idempotencyKey renders the natural key as a comparable string.
func idempotencyKey(workstationID int64, minute time.Time, app string) string {
	return fmt.Sprintf("%d|%s|%s", workstationID,
		minute.UTC().Format("2006-01-02T15:04:05Z"), app)
}
