package repo

import (
	"context"
	"database/sql"
	"errors"

	"github.com/jmoiron/sqlx"
)

var (
	ErrNotFound = errors.New("repo: not found")
	// ErrConflict means an idempotency key already exists with a different
	// payload: the batch must be rejected, never silently overwritten.
	ErrConflict = errors.New("repo: conflicting snapshot for idempotency key")
)

type Repo struct {
	db *sqlx.DB
}

func New(db *sqlx.DB) *Repo { return &Repo{db: db} }

// DB exposes the pool for package-local transactions started by services.
func (r *Repo) DB() *sqlx.DB { return r.db }

// Beginx starts a transaction bound to the given context.
func (r *Repo) BeginTxx(ctx context.Context) (*sqlx.Tx, error) {
	return r.db.BeginTxx(ctx, nil)
}

func (r *Repo) Employee(ctx context.Context, tx sqlx.QueryerContext, id int64) (Employee, error) {
	var e Employee
	if err := sqlx.GetContext(ctx, tx, &e, `SELECT * FROM employees WHERE id = $1`, id); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return Employee{}, ErrNotFound
		}
		return Employee{}, err
	}
	return e, nil
}

func (r *Repo) Workstation(ctx context.Context, tx sqlx.QueryerContext, id int64) (Workstation, error) {
	var w Workstation
	if err := sqlx.GetContext(ctx, tx, &w, `SELECT * FROM workstations WHERE id = $1`, id); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return Workstation{}, ErrNotFound
		}
		return Workstation{}, err
	}
	return w, nil
}

// CurrentPolicy returns the highest-versioned published policy.
func (r *Repo) CurrentPolicy(ctx context.Context, tx sqlx.QueryerContext) (Policy, error) {
	var p Policy
	err := sqlx.GetContext(ctx, tx, &p,
		`SELECT version, window_start_minute, window_end_minute
		   FROM policy_versions ORDER BY version DESC LIMIT 1`)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return Policy{}, ErrNotFound
		}
		return Policy{}, err
	}
	if err := sqlx.SelectContext(ctx, tx, &p.ExcludedPatterns,
		`SELECT pattern FROM policy_excluded_apps WHERE policy_version = $1 ORDER BY pattern`,
		p.Version); err != nil {
		return Policy{}, err
	}
	if err := sqlx.SelectContext(ctx, tx, &p.ExemptDepartments,
		`SELECT department_id FROM policy_exempt_departments WHERE policy_version = $1 ORDER BY department_id`,
		p.Version); err != nil {
		return Policy{}, err
	}
	return p, nil
}

// CurrentClassification returns the rules of the highest classification
// version, already ordered priority DESC, rule_id ASC for stable matching.
func (r *Repo) CurrentClassification(ctx context.Context, tx sqlx.QueryerContext) (Classification, error) {
	var version int
	if err := sqlx.GetContext(ctx, tx, &version,
		`SELECT version FROM classification_versions ORDER BY version DESC LIMIT 1`); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return Classification{}, ErrNotFound
		}
		return Classification{}, err
	}
	var rules []ClassificationRule
	if err := sqlx.SelectContext(ctx, tx, &rules,
		`SELECT rule_id, pattern, category, priority
		   FROM classification_rules WHERE version = $1
		  ORDER BY priority DESC, rule_id ASC`, version); err != nil {
		return Classification{}, err
	}
	return Classification{Version: version, Rules: rules}, nil
}

func (r *Repo) Token(ctx context.Context, token string) (APIToken, error) {
	var t APIToken
	if err := r.db.GetContext(ctx, &t, `SELECT * FROM api_tokens WHERE token = $1`, token); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return APIToken{}, ErrNotFound
		}
		return APIToken{}, err
	}
	return t, nil
}

// ListDepartmentIDs returns every department id (used by full rebuild).
func (r *Repo) ListDepartmentIDs(ctx context.Context) ([]int64, error) {
	var ids []int64
	if err := r.db.SelectContext(ctx, &ids, `SELECT id FROM departments ORDER BY id`); err != nil {
		return nil, err
	}
	return ids, nil
}
