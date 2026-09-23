package service

import (
	"context"
	"time"

	"costlens/internal/auth"
	"costlens/internal/db/dbgen"
	"costlens/internal/money"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"
	"github.com/shopspring/decimal"
)

// Budget is a single immutable budget version returned by the API.
type Budget struct {
	ID           string          `json:"id"`
	CostCenterID string          `json:"cost_center_id"`
	Currency     string          `json:"currency"`
	Period       string          `json:"period"`
	Amount       decimal.Decimal `json:"amount"`
	Version      int32           `json:"version"`
	CreatedAt    time.Time       `json:"created_at"`
}

// CreateBudgetInput is the request body for PUT /budgets.
type CreateBudgetInput struct {
	CostCenterID string          `json:"cost_center_id"`
	Currency     string          `json:"currency"`
	Period       string          `json:"period"` // YYYY-MM-01; normalized to month start
	Amount       decimal.Decimal `json:"amount"`
}

func (in *CreateBudgetInput) validate() (time.Time, error) {
	if in.CostCenterID == "" {
		return time.Time{}, &ValidationError{Code: "invalid_budget", Message: "cost_center_id required"}
	}
	if err := money.ValidateCurrency(in.Currency); err != nil {
		return time.Time{}, &ValidationError{Code: "invalid_budget", Message: err.Error()}
	}
	per, err := time.Parse("2006-01-02", in.Period)
	if err != nil {
		return time.Time{}, &ValidationError{Code: "invalid_budget", Message: "period must be YYYY-MM-DD (day is normalized to 01)"}
	}
	per = monthStart(per)
	if in.Amount.Sign() <= 0 {
		return time.Time{}, &ValidationError{Code: "invalid_budget", Message: "budget amount must be positive"}
	}
	if money.FracDigitsOf(in.Amount) > money.Scale {
		return time.Time{}, &ValidationError{Code: "invalid_budget", Message: "budget amount has more than 6 decimal places"}
	}
	return per, nil
}

// CreateBudget appends a new immutable budget version and immediately evaluates
// alerts against the month-to-date total. Old versions and their alerts remain
// untouched (historical basis).
func (s *Service) CreateBudget(ctx context.Context, p *auth.Principal, in CreateBudgetInput) (*Budget, error) {
	if !p.CanImport() {
		return nil, &ValidationError{Code: "forbidden", Message: "role may not set budgets"}
	}
	per, err := in.validate()
	if err != nil {
		return nil, err
	}

	var out Budget
	err = s.transact(ctx, func(tx pgx.Tx) error {
		q := dbgen.New(tx)
		if err := q.AcquireTxAdvisoryLock(ctx, advisoryLockKey); err != nil {
			return err
		}
		cc, err := q.GetCostCenterScoped(ctx, dbgen.GetCostCenterScopedParams{
			ID:      in.CostCenterID,
			Column2: p.ScopeOrgIDs(),
		})
		if err != nil {
			return &ValidationError{Code: "cost_center_not_found", Message: "cost center not found in authorized organization"}
		}
		_ = cc
		bv, err := q.CreateBudgetVersion(ctx, dbgen.CreateBudgetVersionParams{
			CostCenterID: in.CostCenterID,
			Currency:     in.Currency,
			Period:       per,
			Amount:       in.Amount,
			CreatedBy:    pgtype.Text{String: p.ID, Valid: true},
		})
		if err != nil {
			return err
		}

		run, err := q.CreateAnomalyRun(ctx, dbgen.CreateAnomalyRunParams{
			Kind: "manual", ImportID: pgtype.Text{Valid: false},
		})
		if err != nil {
			return err
		}
		row := dbgen.AllCurrentBudgetsRow{
			ID: bv.ID, CostCenterID: bv.CostCenterID, Currency: bv.Currency,
			Period: bv.Period, Amount: bv.Amount, Version: bv.Version,
		}
		totals, err := q.MonthlyCostCenterTotalsForPeriod(ctx,
			dbgen.MonthlyCostCenterTotalsForPeriodParams{Period: per, Column2: []string{in.CostCenterID}})
		if err != nil {
			return err
		}
		totalByKey := map[budgetKey]dbgen.MonthlyCostCenterSummary{}
		for _, t := range totals {
			totalByKey[budgetKey{t.CostCenterID, t.Currency, dateStr(t.Period)}] = t
		}
		if err := evaluateBudgetRows(ctx, q, []dbgen.AllCurrentBudgetsRow{row}, totalByKey, run.ID); err != nil {
			return err
		}

		out = Budget{
			ID: bv.ID, CostCenterID: bv.CostCenterID, Currency: bv.Currency,
			Period: dateStr(bv.Period), Amount: bv.Amount, Version: bv.Version,
			CreatedAt: bv.CreatedAt,
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return &out, nil
}
