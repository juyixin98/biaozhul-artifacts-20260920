// Package manager serves department-scoped read endpoints. Every query is
// filtered by the department bound to the caller's API key: detail rows,
// summaries and exports use the same isolation, and foreign employees produce
// 404 rather than a distinguishable "exists but forbidden" response.
package manager

import (
	"context"
	"fmt"
	"net/http"
	"time"

	"github.com/jmoiron/sqlx"
)

type APIError struct {
	HTTPStatus int
	Code       string
	Message    string
}

func (e *APIError) Error() string { return fmt.Sprintf("%s: %s", e.Code, e.Message) }

type Principal struct {
	Username     string
	DepartmentID int64
}

type Service struct {
	DB *sqlx.DB
}

func New(db *sqlx.DB) *Service { return &Service{DB: db} }

type DailyRow struct {
	EmployeeID               int64  `json:"employee_id" db:"employee_id"`
	DisplayName              string `json:"display_name" db:"display_name"`
	LocalDate                string `json:"local_date" db:"local_date"`
	ProductiveCount          int64  `json:"productive_count" db:"productive_count"`
	UnproductiveCount        int64  `json:"unproductive_count" db:"unproductive_count"`
	NeutralCount             int64  `json:"neutral_count" db:"neutral_count"`
	TotalCount               int64  `json:"total_count" db:"total_count"`
	PolicyVersionMin         int32  `json:"policy_version_min" db:"policy_version_min"`
	PolicyVersionMax         int32  `json:"policy_version_max" db:"policy_version_max"`
	ClassificationVersionMin int64  `json:"classification_version_min" db:"classification_version_min"`
	ClassificationVersionMax int64  `json:"classification_version_max" db:"classification_version_max"`
}

// DailySummary returns daily rows for one department, optionally one employee.
func (s *Service) DailySummary(ctx context.Context, p Principal, employeeID int64, from, to string) ([]DailyRow, error) {
	if employeeID > 0 {
		if err := s.assertSameDepartment(ctx, p, employeeID); err != nil {
			return nil, err
		}
	}
	if _, err := time.Parse("2006-01-02", from); err != nil {
		return nil, &APIError{http.StatusBadRequest, "invalid_date", "from must be YYYY-MM-DD"}
	}
	if _, err := time.Parse("2006-01-02", to); err != nil {
		return nil, &APIError{http.StatusBadRequest, "invalid_date", "to must be YYYY-MM-DD"}
	}
	var rows []DailyRow
	q := `
		select d.employee_id, e.display_name, d.local_date::text as local_date,
		       d.productive_count, d.unproductive_count, d.neutral_count, d.total_count,
		       d.policy_version_min, d.policy_version_max,
		       d.classification_version_min, d.classification_version_max
		from daily_employee_summary d
		join employees e on e.id = d.employee_id
		where e.department_id = $1
		  and d.local_date between $2::date and $3::date`
	args := []any{p.DepartmentID, from, to}
	if employeeID > 0 {
		q += " and d.employee_id = $4"
		args = append(args, employeeID)
	}
	q += " order by d.employee_id, d.local_date"
	if err := s.DB.SelectContext(ctx, &rows, q, args...); err != nil {
		return nil, err
	}
	return rows, nil
}

type WeeklyRow struct {
	WeekStart         string `json:"week_start" db:"week_start"`
	ProductiveCount   int64  `json:"productive_count" db:"productive_count"`
	UnproductiveCount int64  `json:"unproductive_count" db:"unproductive_count"`
	NeutralCount      int64  `json:"neutral_count" db:"neutral_count"`
	TotalCount        int64  `json:"total_count" db:"total_count"`
}

// WeeklySummary returns the caller's department weekly rows.
func (s *Service) WeeklySummary(ctx context.Context, p Principal, from, to string) ([]WeeklyRow, error) {
	if _, err := time.Parse("2006-01-02", from); err != nil {
		return nil, &APIError{http.StatusBadRequest, "invalid_date", "from must be YYYY-MM-DD"}
	}
	if _, err := time.Parse("2006-01-02", to); err != nil {
		return nil, &APIError{http.StatusBadRequest, "invalid_date", "to must be YYYY-MM-DD"}
	}
	var rows []WeeklyRow
	if err := s.DB.SelectContext(ctx, &rows, `
		select week_start::text as week_start, productive_count, unproductive_count,
		       neutral_count, total_count
		from weekly_department_summary
		where department_id = $1 and week_start between $2::date and $3::date
		order by week_start`, p.DepartmentID, from, to); err != nil {
		return nil, err
	}
	return rows, nil
}

type DetailRow struct {
	BucketTime            string `json:"bucket_time_utc" db:"bucket_time"`
	EmployeeID            int64  `json:"employee_id" db:"employee_id"`
	WorkstationID         string `json:"workstation_id" db:"workstation_id"`
	AppName               string `json:"app_name" db:"app_name"`
	ActivityCount         int    `json:"activity_count" db:"activity_count"`
	Category              string `json:"category" db:"category"`
	MatchedRuleID         string `json:"matched_rule_id" db:"matched_rule_id"`
	PolicyVersion         int32  `json:"policy_version" db:"policy_version"`
	ClassificationVersion int64  `json:"classification_version" db:"classification_version"`
}

// DetailRows returns stored raw snapshots for one employee in a UTC interval.
// Filtered snapshots do not exist in the table and therefore can never be
// exported.
func (s *Service) DetailRows(ctx context.Context, p Principal, employeeID int64, fromUTC, toUTC time.Time) ([]DetailRow, error) {
	if employeeID <= 0 {
		return nil, &APIError{http.StatusBadRequest, "employee_required", "employee_id query parameter is required"}
	}
	if err := s.assertSameDepartment(ctx, p, employeeID); err != nil {
		return nil, err
	}
	if !fromUTC.Before(toUTC) {
		return nil, &APIError{http.StatusBadRequest, "invalid_range", "from must be before to"}
	}
	var rows []DetailRow
	if err := s.DB.SelectContext(ctx, &rows, `
		select to_char(bucket_time at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') as bucket_time,
		       employee_id, workstation_id, app_name, activity_count,
		       category, coalesce(matched_rule_id,'') as matched_rule_id,
		       policy_version, classification_version
		from activity_snapshots
		where employee_id = $1 and bucket_time >= $2 and bucket_time < $3
		order by bucket_time`, employeeID, fromUTC, toUTC); err != nil {
		return nil, err
	}
	return rows, nil
}

func (s *Service) assertSameDepartment(ctx context.Context, p Principal, employeeID int64) error {
	var dept int64
	err := s.DB.QueryRowxContext(ctx,
		`select department_id from employees where id = $1`, employeeID).Scan(&dept)
	if err != nil || dept != p.DepartmentID {
		// Same response whether the employee does not exist or belongs to
		// another department: do not reveal other departments' employees.
		return &APIError{http.StatusNotFound, "employee_not_found",
			fmt.Sprintf("employee %d not found in your department", employeeID)}
	}
	return nil
}
