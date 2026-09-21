package repo

import (
	"context"
)

type NewPolicy struct {
	WindowStartMinute int
	WindowEndMinute   int
	ExcludedPatterns  []string
	ExemptDepartments []int64
}

type NewClassification struct {
	Rules []ClassificationRule
}

// PublishPolicy creates the next policy version atomically. It only ever
// affects subsequent ingestion; existing raw rows keep their policy_version.
func (r *Repo) PublishPolicy(ctx context.Context, p NewPolicy) (Policy, error) {
	tx, err := r.db.BeginTxx(ctx, nil)
	if err != nil {
		return Policy{}, err
	}
	defer tx.Rollback()

	var version int
	if err := tx.GetContext(ctx, &version,
		`SELECT COALESCE(MAX(version), 0) + 1 FROM policy_versions`); err != nil {
		return Policy{}, err
	}
	if _, err := tx.ExecContext(ctx,
		`INSERT INTO policy_versions (version, window_start_minute, window_end_minute)
		 VALUES ($1, $2, $3)`, version, p.WindowStartMinute, p.WindowEndMinute); err != nil {
		return Policy{}, err
	}
	for _, pat := range p.ExcludedPatterns {
		if _, err := tx.ExecContext(ctx,
			`INSERT INTO policy_excluded_apps (policy_version, pattern) VALUES ($1, $2)
			  ON CONFLICT DO NOTHING`, version, pat); err != nil {
			return Policy{}, err
		}
	}
	for _, dep := range p.ExemptDepartments {
		if _, err := tx.ExecContext(ctx,
			`INSERT INTO policy_exempt_departments (policy_version, department_id) VALUES ($1, $2)
			  ON CONFLICT DO NOTHING`, version, dep); err != nil {
			return Policy{}, err
		}
	}
	if err := tx.Commit(); err != nil {
		return Policy{}, err
	}
	return Policy{
		Version:           version,
		WindowStartMinute: p.WindowStartMinute,
		WindowEndMinute:   p.WindowEndMinute,
		ExcludedPatterns:  p.ExcludedPatterns,
		ExemptDepartments: p.ExemptDepartments,
	}, nil
}

// PublishClassification freezes the next immutable rule set version.
func (r *Repo) PublishClassification(ctx context.Context, c NewClassification) (int, error) {
	tx, err := r.db.BeginTxx(ctx, nil)
	if err != nil {
		return 0, err
	}
	defer tx.Rollback()

	var version int
	if err := tx.GetContext(ctx, &version,
		`SELECT COALESCE(MAX(version), 0) + 1 FROM classification_versions`); err != nil {
		return 0, err
	}
	if _, err := tx.ExecContext(ctx,
		`INSERT INTO classification_versions (version) VALUES ($1)`, version); err != nil {
		return 0, err
	}
	for _, rule := range c.Rules {
		if _, err := tx.ExecContext(ctx,
			`INSERT INTO classification_rules (version, rule_id, pattern, category, priority)
			 VALUES ($1, $2, $3, $4, $5)`,
			version, rule.RuleID, rule.Pattern, rule.Category, rule.Priority); err != nil {
			return 0, err
		}
	}
	return version, tx.Commit()
}
