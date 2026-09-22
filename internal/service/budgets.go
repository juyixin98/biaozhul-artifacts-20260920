package service

import (
	"context"
	"errors"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/shopspring/decimal"

	"costlens/internal/decimalx"
)

var thresholds = []int32{50, 75, 90, 100}

// evaluateBudgets fires, exactly once per (budget version, threshold), every
// threshold the month-to-date spend has now crossed. Only active budgets for
// affected (cost-center, month, currency) slices are considered.
// ON CONFLICT DO NOTHING + unique(budget_id, threshold_pct) makes the
// "first crossing only" guarantee hold even across concurrent transactions.
func evaluateBudgets(ctx context.Context, tx pgx.Tx, accountID, batchID int64) error {
	rows, err := tx.Query(ctx, `
		SELECT b.id, b.month::date, b.currency::text, b.monthly_limit::text
		FROM budgets b
		WHERE b.active
		  AND (b.cost_center_id, b.month, b.currency) IN (
		      SELECT DISTINCT a.cost_center_id,
		             date_trunc('month', s.usage_date)::date, s.currency
		      FROM stage_rows s
		      JOIN accounts a ON a.id = $1)
		FOR UPDATE OF b`, accountID)
	if err != nil {
		return err
	}
	type activeBudget struct {
		id       int64
		month    time.Time
		currency string
		limit    decimal.Decimal
	}
	var budgetsList []activeBudget
	for rows.Next() {
		var b activeBudget
		var cur, lim string
		if err := rows.Scan(&b.id, &b.month, &cur, &lim); err != nil {
			rows.Close()
			return err
		}
		b.currency = strings.TrimSpace(cur)
		b.limit, _ = decimal.NewFromString(lim)
		budgetsList = append(budgetsList, b)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return err
	}

	for _, b := range budgetsList {
		if err := fireBudgetAlerts(ctx, tx, b.id, b.month, b.currency, b.limit, batchID); err != nil {
			return err
		}
	}
	return nil
}

// fireBudgetAlerts inserts, once each, every threshold the month-to-date
// spend has crossed for one specific budget version. Used both during import
// (live crossings) and at budget creation (thresholds already past).
func fireBudgetAlerts(ctx context.Context, tx pgx.Tx, budgetID int64,
	month time.Time, currency string, limit decimal.Decimal, batchID int64) error {
	var spentText string
	if err := tx.QueryRow(ctx, `
		SELECT COALESCE(round(sum(r.amount), 6), 0)::text
		FROM billing_records r
		JOIN accounts a ON a.id = r.account_id
		JOIN budgets bu ON bu.id = $1
		WHERE a.cost_center_id = bu.cost_center_id
		  AND r.currency = bu.currency
		  AND date_trunc('month', r.usage_date)::date = bu.month`,
		budgetID).Scan(&spentText); err != nil {
		return err
	}
	spent, _ := decimal.NewFromString(spentText)
	ratio := spent.Div(limit).Mul(decimal.NewFromInt(100))
	var batchRef any
	if batchID != 0 {
		batchRef = batchID
	}
	for _, pct := range thresholds {
		if ratio.GreaterThanOrEqual(decimal.NewFromInt32(pct)) {
			if _, err := tx.Exec(ctx, `
				INSERT INTO budget_alerts
				    (budget_id, threshold_pct, spent_amount, month, currency,
				     triggered_by_batch)
				VALUES ($1,$2,$3,$4,$5,$6)
				ON CONFLICT (budget_id, threshold_pct) DO NOTHING`,
				budgetID, pct, decimalx.ToNumeric(spent), pgDate(month),
				currency, batchRef); err != nil {
				return err
			}
		}
	}
	return nil
}

// SetBudgetInput creates a new immutable budget version and deactivates the
// previous one. Historical alerts remain attached to the old version.
type SetBudgetInput struct {
	CostCenterID int64  `json:"cost_center_id"`
	Currency     string `json:"currency"`
	Month        string `json:"month"` // YYYY-MM
	MonthlyLimit string `json:"monthly_limit"`
}

func (s *Service) SetBudget(ctx context.Context, in SetBudgetInput, actorID int64) (*BudgetDTO, error) {
	month, err := time.Parse("2006-01", in.Month)
	if err != nil {
		return nil, errors.New("month must be YYYY-MM")
	}
	limit, err := decimal.NewFromString(in.MonthlyLimit)
	if err != nil || !limit.IsPositive() {
		return nil, errors.New("monthly_limit must be a positive fixed-point decimal")
	}
	if len(in.Currency) != 3 {
		return nil, errors.New("currency must be a 3-letter code")
	}

	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx)
	if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock($1)`, advisoryLockID); err != nil {
		return nil, err
	}

	var nextVersion int32
	if err := tx.QueryRow(ctx,
		`SELECT COALESCE(max(version),0)+1 FROM budgets
		 WHERE cost_center_id=$1 AND currency=$2 AND month=$3`,
		in.CostCenterID, in.Currency, pgDate(month)).Scan(&nextVersion); err != nil {
		return nil, err
	}
	// Deactivate any active older version (partial unique index guarantees
	// at most one, but do this generically).
	if _, err := tx.Exec(ctx,
		`UPDATE budgets SET active=false
		 WHERE cost_center_id=$1 AND currency=$2 AND month=$3 AND active`,
		in.CostCenterID, in.Currency, pgDate(month)); err != nil {
		return nil, err
	}
	var createdBy *int64
	if actorID != 0 {
		v := actorID
		createdBy = &v
	}
	row := tx.QueryRow(ctx, `
		INSERT INTO budgets
		    (cost_center_id, currency, month, version, monthly_limit, created_by)
		VALUES ($1,$2,$3,$4,$5,$6)
		RETURNING id, created_at`,
		in.CostCenterID, in.Currency, pgDate(month), nextVersion,
		decimalx.ToNumeric(limit), createdBy)
	var id int64
	var createdAt time.Time
	if err := row.Scan(&id, &createdAt); err != nil {
		return nil, err
	}
	// Evaluate thresholds already crossed by month-to-date spend so creating a
	// budget mid-month never silently misses prior crossings. ON CONFLICT keeps
	// this idempotent (each version fires a threshold at most once).
	if err := fireBudgetAlerts(ctx, tx, id, month, in.Currency, limit, 0); err != nil {
		return nil, err
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, err
	}
	return &BudgetDTO{
		ID: id, CostCenterID: in.CostCenterID, Currency: in.Currency,
		Month: in.Month, Version: nextVersion,
		MonthlyLimit: limit.StringFixed(6), Active: true, CreatedAt: createdAt,
	}, nil
}

func (s *Service) ListBudgets(ctx context.Context, ccIDs []int64) ([]*BudgetDTO, error) {
	bs, err := s.q.ListBudgetVersions(ctx, ccIDs)
	if err != nil {
		return nil, err
	}
	out := make([]*BudgetDTO, 0, len(bs))
	for _, b := range bs {
		month := b.Month.Time.Format("2006-01")
		out = append(out, &BudgetDTO{
			ID: b.ID, CostCenterID: b.CostCenterID, Currency: strings.TrimSpace(b.Currency),
			Month: month, Version: b.Version,
			MonthlyLimit: decimalx.Fixed(b.MonthlyLimit, 6),
			Active:       b.Active, CreatedAt: b.CreatedAt.Time,
		})
	}
	return out, nil
}

func (s *Service) ListBudgetAlerts(ctx context.Context, ccIDs []int64) ([]*BudgetAlertDTO, error) {
	rows, err := s.q.ListBudgetAlerts(ctx, ccIDs)
	if err != nil {
		return nil, err
	}
	out := make([]*BudgetAlertDTO, 0, len(rows))
	for _, a := range rows {
		out = append(out, &BudgetAlertDTO{
			ID: a.ID, BudgetID: a.BudgetID, BudgetVersion: a.BudgetVersion,
			CostCenterID: a.CostCenterID, ThresholdPct: a.ThresholdPct,
			SpentAmount: decimalx.Fixed(a.SpentAmount, 6),
			Month:       a.Month.Time.Format("2006-01"),
			Currency:    strings.TrimSpace(a.Currency),
			TriggeredAt: a.TriggeredAt.Time,
		})
	}
	return out, nil
}
