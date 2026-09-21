package aggregate

import (
	"context"
	"database/sql"
	"errors"
	"time"

	"github.com/jmoiron/sqlx"

	"desklens/internal/repo"
)

// ErrPartialCoverage means raw data for the key is missing (purged), so the
// retained summary must not be recomputed from the incomplete remainder.
var ErrPartialCoverage = errors.New("aggregate: raw coverage incomplete; refusing to overwrite summary")

type RebuildReport struct {
	DaysProcessed   int      `json:"days_processed"`
	DaysSkipped     int      `json:"days_skipped_partial"`
	WeeksProcessed  int      `json:"weeks_processed"`
	WeeksSkipped    int      `json:"weeks_skipped_partial"`
	SkippedDayKeys  []string `json:"-"`
	SkippedWeekKeys []string `json:"-"`
}

type Service struct {
	repo *repo.Repo
}

func NewService(r *repo.Repo) *Service { return &Service{repo: r} }

// RebuildDaily rebuilds one employee/day from raw rows. It refuses to touch
// summaries whose raw coverage is partial (after a purge) so incomplete raw
// history can never overwrite a complete statistic.
func (s *Service) RebuildDaily(ctx context.Context, employeeID int64, day time.Time) error {
	tx, err := s.repo.BeginTxx(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback()

	full, err := s.checkDay(ctx, tx, employeeID, day)
	if err != nil {
		return err
	}
	if !full {
		return ErrPartialCoverage
	}
	if err := repo.LockDaily(ctx, tx, employeeID, day); err != nil {
		return err
	}
	// Re-check under the lock: a concurrent purge could have landed between
	// the first check and acquiring it.
	full, err = s.checkDay(ctx, tx, employeeID, day)
	if err != nil {
		return err
	}
	if !full {
		return ErrPartialCoverage
	}
	if err := s.repo.RecomputeDaily(ctx, tx, employeeID, day); err != nil {
		return err
	}
	return tx.Commit()
}

func (s *Service) checkDay(ctx context.Context, tx *sqlx.Tx, employeeID int64, day time.Time) (bool, error) {
	var state string
	err := tx.QueryRowxContext(ctx,
		`SELECT raw_state FROM employee_day_coverage
		  WHERE employee_id = $1 AND local_date = $2`, employeeID, day).Scan(&state)
	if errors.Is(err, sql.ErrNoRows) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	return state == "full", nil
}

// RebuildWeekly rebuilds one department/week, gated on every employee/day that
// raw data covers still having full coverage.
func (s *Service) RebuildWeekly(ctx context.Context, departmentID int64, week time.Time) error {
	tx, err := s.repo.BeginTxx(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback()

	ok, err := s.weekComplete(ctx, tx, departmentID, week)
	if err != nil {
		return err
	}
	if !ok {
		return ErrPartialCoverage
	}
	if err := repo.LockWeekly(ctx, tx, departmentID, week); err != nil {
		return err
	}
	ok, err = s.weekComplete(ctx, tx, departmentID, week)
	if err != nil {
		return err
	}
	if !ok {
		return ErrPartialCoverage
	}
	if err := s.repo.RecomputeWeekly(ctx, tx, departmentID, week); err != nil {
		return err
	}
	return tx.Commit()
}

func (s *Service) weekComplete(ctx context.Context, tx *sqlx.Tx, departmentID int64, week time.Time) (bool, error) {
	// Any partially-purged day anywhere in the week blocks the rebuild.
	partial, err := s.repo.WeekHasPartial(ctx, tx, departmentID, week)
	if err != nil {
		return false, err
	}
	if partial {
		return false, nil
	}
	covered, present, err := s.repo.FullDaysInWeek(ctx, tx, departmentID, week)
	if err != nil {
		return false, err
	}
	if len(present) == 0 {
		// No raw rows for this week: rebuilding would yield nothing, and an
		// existing summary (purged) must not be erased.
		return false, nil
	}
	for k := range present {
		if !covered[k] {
			return false, nil
		}
	}
	return true, nil
}

// RebuildAll rebuilds every day and week that still has complete raw
// coverage. Each key is its own transaction: one partial day only skips that
// key. Ingestion for the same keys serializes on the same advisory locks, so
// commits arriving during the rebuild are neither lost nor double counted.
func (s *Service) RebuildAll(ctx context.Context) (RebuildReport, error) {
	var report RebuildReport

	dayKeys, err := s.repo.AllDayKeys(ctx)
	if err != nil {
		return report, err
	}
	for _, k := range dayKeys {
		employeeID, _ := k[0].(int64)
		day, _ := k[1].(time.Time)
		err := s.RebuildDaily(ctx, employeeID, day)
		switch {
		case err == nil:
			report.DaysProcessed++
		case errors.Is(err, ErrPartialCoverage):
			report.DaysSkipped++
			report.SkippedDayKeys = append(report.SkippedDayKeys,
				day.Format("2006-01-02")+"#employee:"+itoa(employeeID))
		default:
			return report, err
		}
	}

	weekKeys, err := s.repo.AllWeekKeys(ctx)
	if err != nil {
		return report, err
	}
	for _, k := range weekKeys {
		departmentID, _ := k[0].(int64)
		week, _ := k[1].(time.Time)
		err := s.RebuildWeekly(ctx, departmentID, week)
		switch {
		case err == nil:
			report.WeeksProcessed++
		case errors.Is(err, ErrPartialCoverage):
			report.WeeksSkipped++
			report.SkippedWeekKeys = append(report.SkippedWeekKeys,
				week.Format("2006-01-02")+"#department:"+itoa(departmentID))
		default:
			return report, err
		}
	}
	return report, nil
}

func itoa(i int64) string {
	if i == 0 {
		return "0"
	}
	var b [20]byte
	p := len(b)
	for i > 0 {
		p--
		b[p] = byte('0' + i%10)
		i /= 10
	}
	return string(b[p:])
}
