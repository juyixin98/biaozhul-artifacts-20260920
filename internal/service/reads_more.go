package service

import (
	"context"

	"costlens/internal/auth"
	"costlens/internal/db/dbgen"

	"github.com/jackc/pgx/v5"
)

// ---- Summaries (all currency-grouped, never mixed) ----

func (s *Service) DailyAccount(ctx context.Context, p *auth.Principal, orgIDs []string, f Filters) ([]dbgen.DailyAccountSummary, error) {
	orgIDs = filterScope(p, orgIDs)
	if len(orgIDs) == 0 {
		return nil, nil
	}
	var out []dbgen.DailyAccountSummary
	err := s.readTx(ctx, func(tx pgx.Tx) error {
		var err error
		out, err = dbgen.New(tx).ListDailyAccountScoped(ctx, dbgen.ListDailyAccountScopedParams{
			Column1: orgIDs, AccountID: auth.TextNull(f.AccountID),
			DateFrom: f.dateFrom(), DateTo: f.dateTo(), Currency: auth.TextNull(f.Currency),
		})
		return err
	})
	return out, err
}

func (s *Service) MonthlyAccount(ctx context.Context, p *auth.Principal, orgIDs []string, f Filters) ([]dbgen.MonthlyAccountSummary, error) {
	orgIDs = filterScope(p, orgIDs)
	if len(orgIDs) == 0 {
		return nil, nil
	}
	f = monthRange(f)
	var out []dbgen.MonthlyAccountSummary
	err := s.readTx(ctx, func(tx pgx.Tx) error {
		var err error
		out, err = dbgen.New(tx).ListMonthlyAccountScoped(ctx, dbgen.ListMonthlyAccountScopedParams{
			Column1: orgIDs, AccountID: auth.TextNull(f.AccountID),
			DateFrom: f.dateFrom(), DateTo: f.dateTo(), Currency: auth.TextNull(f.Currency),
		})
		return err
	})
	return out, err
}

func (s *Service) DailyCostCenter(ctx context.Context, p *auth.Principal, orgIDs []string, f Filters) ([]dbgen.DailyCostCenterSummary, error) {
	orgIDs = filterScope(p, orgIDs)
	if len(orgIDs) == 0 {
		return nil, nil
	}
	var out []dbgen.DailyCostCenterSummary
	err := s.readTx(ctx, func(tx pgx.Tx) error {
		var err error
		out, err = dbgen.New(tx).ListDailyCostCenterScoped(ctx, dbgen.ListDailyCostCenterScopedParams{
			Column1: orgIDs, CostCenterID: auth.TextNull(f.CostCenterID),
			DateFrom: f.dateFrom(), DateTo: f.dateTo(), Currency: auth.TextNull(f.Currency),
		})
		return err
	})
	return out, err
}

func (s *Service) MonthlyCostCenter(ctx context.Context, p *auth.Principal, orgIDs []string, f Filters) ([]dbgen.MonthlyCostCenterSummary, error) {
	orgIDs = filterScope(p, orgIDs)
	if len(orgIDs) == 0 {
		return nil, nil
	}
	f = monthRange(f)
	var out []dbgen.MonthlyCostCenterSummary
	err := s.readTx(ctx, func(tx pgx.Tx) error {
		var err error
		out, err = dbgen.New(tx).ListMonthlyCostCenterScoped(ctx, dbgen.ListMonthlyCostCenterScopedParams{
			Column1: orgIDs, CostCenterID: auth.TextNull(f.CostCenterID),
			DateFrom: f.dateFrom(), DateTo: f.dateTo(), Currency: auth.TextNull(f.Currency),
		})
		return err
	})
	return out, err
}

// monthRange turns an exact `period` filter (month start) into the from/to
// bounds of that single month for monthly summary queries.
func monthRange(f Filters) Filters {
	if !f.Period.IsZero() {
		f.DateFrom = monthStart(f.Period)
		f.DateTo = monthEnd(f.Period)
	}
	return f
}

// ---- Budgets & alerts ----

func (s *Service) ListBudgets(ctx context.Context, p *auth.Principal, orgIDs []string, f Filters) ([]dbgen.BudgetVersion, error) {
	orgIDs = filterScope(p, orgIDs)
	if len(orgIDs) == 0 {
		return nil, nil
	}
	var out []dbgen.BudgetVersion
	err := s.readTx(ctx, func(tx pgx.Tx) error {
		var err error
		period := pgtypeDateNull()
		if !f.Period.IsZero() {
			period = pgtypeDateOf(monthStart(f.Period))
		}
		out, err = dbgen.New(tx).ListCurrentBudgetsScoped(ctx, dbgen.ListCurrentBudgetsScopedParams{
			Column1:      orgIDs,
			CostCenterID: auth.TextNull(f.CostCenterID),
			Currency:     auth.TextNull(f.Currency),
			Period:       period,
		})
		return err
	})
	return out, err
}

func (s *Service) ListBudgetAlerts(ctx context.Context, p *auth.Principal, orgIDs []string, f Filters) ([]dbgen.BudgetAlert, error) {
	orgIDs = filterScope(p, orgIDs)
	if len(orgIDs) == 0 {
		return nil, nil
	}
	var out []dbgen.BudgetAlert
	err := s.readTx(ctx, func(tx pgx.Tx) error {
		var err error
		period := pgtypeDateNull()
		if !f.Period.IsZero() {
			period = pgtypeDateOf(monthStart(f.Period))
		}
		out, err = dbgen.New(tx).ListBudgetAlertsScoped(ctx, dbgen.ListBudgetAlertsScopedParams{
			Column1: orgIDs, CostCenterID: auth.TextNull(f.CostCenterID),
			Currency: auth.TextNull(f.Currency), Period: period,
		})
		return err
	})
	return out, err
}

// ---- Anomalies ----

func (s *Service) ListLatestAnomalies(ctx context.Context, p *auth.Principal, orgIDs []string, f Filters) ([]dbgen.LatestAnomalyAlert, error) {
	orgIDs = filterScope(p, orgIDs)
	if len(orgIDs) == 0 {
		return nil, nil
	}
	var out []dbgen.LatestAnomalyAlert
	err := s.readTx(ctx, func(tx pgx.Tx) error {
		var err error
		out, err = dbgen.New(tx).ListLatestAnomalyAlertsScoped(ctx, dbgen.ListLatestAnomalyAlertsScopedParams{
			Column1: orgIDs, AccountID: auth.TextNull(f.AccountID),
			DateFrom: f.dateFrom(), DateTo: f.dateTo(), Currency: auth.TextNull(f.Currency),
		})
		return err
	})
	return out, err
}

func (s *Service) ListAnomalyHistory(ctx context.Context, p *auth.Principal, orgIDs []string, f Filters) ([]dbgen.ListAnomalyHistoryScopedRow, error) {
	orgIDs = filterScope(p, orgIDs)
	if len(orgIDs) == 0 {
		return nil, nil
	}
	var out []dbgen.ListAnomalyHistoryScopedRow
	err := s.readTx(ctx, func(tx pgx.Tx) error {
		var err error
		out, err = dbgen.New(tx).ListAnomalyHistoryScoped(ctx, dbgen.ListAnomalyHistoryScopedParams{
			Column1: orgIDs, AccountID: auth.TextNull(f.AccountID),
			DateFrom: f.dateFrom(), DateTo: f.dateTo(), Currency: auth.TextNull(f.Currency),
		})
		return err
	})
	return out, err
}

// ---- Imports audit log ----

func (s *Service) ListImports(ctx context.Context, p *auth.Principal, orgIDs []string, limit, offset int32) (any, int64, error) {
	if limit == 0 || limit > 200 {
		limit = 50
	}
	var rows any
	var total int64
	err := s.readTx(ctx, func(tx pgx.Tx) error {
		q := dbgen.New(tx)
		orgIDs = filterScope(p, orgIDs)
		if len(orgIDs) == 0 {
			rows = []dbgen.CostImport{}
			return nil
		}
		r, err := q.ListImportsScoped(ctx, dbgen.ListImportsScopedParams{
			Column1: orgIDs, Limit: limit, Offset: offset,
		})
		if err != nil {
			return err
		}
		rows = r
		total, err = q.CountImportsScoped(ctx, orgIDs)
		return err
	})
	return rows, total, err
}
