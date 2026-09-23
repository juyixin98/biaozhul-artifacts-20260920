package service

import (
	"context"
	"time"

	"costlens/internal/db/dbgen"

	"github.com/jackc/pgx/v5/pgtype"
	"github.com/shopspring/decimal"
)

// Thresholds at which the first crossing produces an alert.
//
// Crossing means actual/budget >= threshold (spending equal to exactly 50% of
// the budget raises the 50% alert). Once an alert for a given budget version
// and threshold exists it can never be duplicated (unique constraint +
// ON CONFLICT DO NOTHING). A new budget version has its own alert history.
var thresholds = []decimal.Decimal{
	decimal.RequireFromString("0.5"),
	decimal.RequireFromString("0.75"),
	decimal.RequireFromString("0.9"),
	decimal.RequireFromString("1"),
}

type budgetKey struct {
	cc       string
	currency string
	period   string
}

// evaluateBudgets checks current budget versions for the cartesian product of
// affected cost centers and months. Currencies are taken from monthly totals,
// so different currencies are never mixed.
func evaluateBudgets(ctx context.Context, q *dbgen.Queries, ccIDs []string, periods []time.Time, runID string) error {
	if len(ccIDs) == 0 || len(periods) == 0 {
		return nil
	}

	totalByKey := map[budgetKey]dbgen.MonthlyCostCenterSummary{}
	curSet := map[string]bool{}
	for _, per := range periods {
		rows, err := q.MonthlyCostCenterTotalsForPeriod(ctx,
			dbgen.MonthlyCostCenterTotalsForPeriodParams{Period: per, Column2: ccIDs})
		if err != nil {
			return err
		}
		for _, r := range rows {
			totalByKey[budgetKey{r.CostCenterID, r.Currency, dateStr(r.Period)}] = r
			curSet[r.Currency] = true
		}
	}
	if len(curSet) == 0 {
		return nil
	}

	slices := buildSlices(ccIDs, mapKeys(curSet), periods)
	budgets, err := q.CurrentBudgetsForSlices(ctx, slices)
	if err != nil {
		return err
	}

	all := make([]dbgen.AllCurrentBudgetsRow, len(budgets))
	for i, b := range budgets {
		all[i] = dbgen.AllCurrentBudgetsRow(b)
	}
	return evaluateBudgetRows(ctx, q, all, totalByKey, runID)
}

func buildSlices(ccIDs, currencies []string, periods []time.Time) dbgen.CurrentBudgetsForSlicesParams {
	p := dbgen.CurrentBudgetsForSlicesParams{}
	for _, cc := range ccIDs {
		for _, cur := range currencies {
			for _, per := range periods {
				p.Column1 = append(p.Column1, cc)
				p.Column2 = append(p.Column2, cur)
				p.Column3 = append(p.Column3, dateStr(per))
			}
		}
	}
	return p
}

// evaluateBudgetRows inserts first-crossing alerts for budgets whose current
// total crosses a threshold. Missing totals count as zero (no crossing for a
// positive budget).
func evaluateBudgetRows(
	ctx context.Context,
	q *dbgen.Queries,
	budgets []dbgen.AllCurrentBudgetsRow,
	totalByKey map[budgetKey]dbgen.MonthlyCostCenterSummary,
	runID string,
) error {
	for _, b := range budgets {
		amt := decimal.Zero
		if t, ok := totalByKey[budgetKey{b.CostCenterID, b.Currency, dateStr(b.Period)}]; ok {
			amt = t.Total
		}
		if b.Amount.IsZero() || amt.IsZero() {
			continue
		}
		ratio := amt.Div(b.Amount)
		for _, th := range thresholds {
			if !ratio.GreaterThanOrEqual(th) {
				continue
			}
			if _, err := q.InsertBudgetAlert(ctx, dbgen.InsertBudgetAlertParams{
				BudgetVersionID: b.ID,
				CostCenterID:    b.CostCenterID,
				Currency:        b.Currency,
				Period:          b.Period,
				Threshold:       th,
				ActualAmount:    amt,
				TriggeredByRun:  pgtype.Text{String: runID, Valid: runID != ""},
			}); err != nil {
				return err
			}
		}
	}
	return nil
}

// evaluateAllBudgets is the rebuild path: every current budget version.
func evaluateAllBudgets(ctx context.Context, q *dbgen.Queries, runID string) error {
	budgets, err := q.AllCurrentBudgets(ctx)
	if err != nil {
		return err
	}
	totalByKey := map[budgetKey]dbgen.MonthlyCostCenterSummary{}
	periodCC := map[string][]string{} // period -> distinct cc list
	periodOrder := []string{}
	for _, b := range budgets {
		ps := dateStr(b.Period)
		if _, ok := periodCC[ps]; !ok {
			periodOrder = append(periodOrder, ps)
		}
		periodCC[ps] = append(periodCC[ps], b.CostCenterID)
	}
	for _, ps := range periodOrder {
		per, err := time.Parse("2006-01-02", ps)
		if err != nil {
			return err
		}
		rows, err := q.MonthlyCostCenterTotalsForPeriod(ctx,
			dbgen.MonthlyCostCenterTotalsForPeriodParams{Period: truncDate(per), Column2: unique(periodCC[ps])})
		if err != nil {
			return err
		}
		for _, r := range rows {
			totalByKey[budgetKey{r.CostCenterID, r.Currency, dateStr(r.Period)}] = r
		}
	}
	return evaluateBudgetRows(ctx, q, budgets, totalByKey, runID)
}

func unique(in []string) []string {
	set := map[string]bool{}
	out := in[:0]
	for _, v := range in {
		if !set[v] {
			set[v] = true
			out = append(out, v)
		}
	}
	return out
}
