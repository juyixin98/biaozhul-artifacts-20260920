// Package store provides read access to reference data and the currently
// published policy / classification versions.
package store

import (
	"context"
	"database/sql"
	"fmt"

	"github.com/jmoiron/sqlx"

	"desklens/internal/model"
)

type Store struct {
	DB *sqlx.DB
}

func New(db *sqlx.DB) *Store { return &Store{DB: db} }

// GetCurrentPolicy loads the highest published policy version together with
// its excluded-app patterns and exempt-department set.
func (s *Store) GetCurrentPolicy(ctx context.Context, q sqlx.QueryerContext) (*model.Policy, error) {
	var p struct {
		Version     int32 `db:"version"`
		StartMinute int   `db:"monitoring_start_minute"`
		EndMinute   int   `db:"monitoring_end_minute"`
	}
	if err := sqlx.GetContext(ctx, q, &p, `
		select version, monitoring_start_minute, monitoring_end_minute
		from policy_versions order by version desc limit 1`); err != nil {
		if err == sql.ErrNoRows {
			return nil, ErrNoPolicy
		}
		return nil, err
	}

	var patterns []string
	if err := sqlx.SelectContext(ctx, q, &patterns, `
		select pattern from policy_excluded_apps
		where policy_version = $1 order by position`, p.Version); err != nil {
		return nil, err
	}

	var deptIDs []int64
	if err := sqlx.SelectContext(ctx, q, &deptIDs, `
		select department_id from policy_exempt_departments
		where policy_version = $1`, p.Version); err != nil {
		return nil, err
	}
	exempt := make(map[int64]bool, len(deptIDs))
	for _, id := range deptIDs {
		exempt[id] = true
	}

	return &model.Policy{
		Version:            p.Version,
		StartMinute:        p.StartMinute,
		EndMinute:          p.EndMinute,
		ExcludedPatterns:   patterns,
		ExemptDepartmentID: exempt,
	}, nil
}

// GetClassification loads a classification version with its rules ordered for
// deterministic first-match evaluation (priority ASC, rule_id ASC).
func (s *Store) GetClassification(ctx context.Context, q sqlx.QueryerContext) (*model.Classification, error) {
	var version int64
	if err := sqlx.GetContext(ctx, q, &version, `
		select version from classification_versions order by version desc limit 1`); err != nil {
		if err == sql.ErrNoRows {
			return nil, ErrNoClassification
		}
		return nil, err
	}
	var rules []model.Rule
	if err := sqlx.SelectContext(ctx, q, &rules, `
		select rule_id, pattern, category, priority
		from classification_rules
		where classification_version = $1
		order by priority asc, rule_id asc`, version); err != nil {
		return nil, err
	}
	return &model.Classification{Version: version, Rules: rules}, nil
}

// GetWorkstation returns the workstation and its employee's department/timezone.
func (s *Store) GetWorkstation(ctx context.Context, q sqlx.QueryerContext, workstationID string) (
	empID, deptID int64, tz string, err error,
) {
	row := struct {
		EmployeeID   int64  `db:"employee_id"`
		DepartmentID int64  `db:"department_id"`
		Timezone     string `db:"timezone"`
	}{}
	err = sqlx.GetContext(ctx, q, &row, `
		select w.employee_id, e.department_id, e.timezone
		from workstations w
		join employees e on e.id = w.employee_id
		where w.id = $1`, workstationID)
	if err == sql.ErrNoRows {
		return 0, 0, "", ErrWorkstationNotFound
	}
	if err != nil {
		return 0, 0, "", err
	}
	return row.EmployeeID, row.DepartmentID, row.Timezone, nil
}

// Common sentinel errors.
var (
	ErrNoPolicy            = fmt.Errorf("no published policy")
	ErrNoClassification    = fmt.Errorf("no published classification")
	ErrWorkstationNotFound = fmt.Errorf("workstation not found")
)
