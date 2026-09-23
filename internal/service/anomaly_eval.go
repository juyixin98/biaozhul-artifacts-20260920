package service

import (
	"context"
	"time"

	"costlens/internal/db/dbgen"
	"costlens/internal/stats"

	"github.com/shopspring/decimal"
)

// evaluateAnomalies appends a new set of anomaly evaluations for every calendar
// day in [from, to] for every (account, currency) that ever recorded spend,
// zero-filling days without a bill line. It is used both by the import path
// (from = earliest imported day, to = latest + 30, so late data re-baselines
// every day whose 30-day window it touches, including the imported days
// themselves) and the rebuild path (from = earliest bill, to = latest bill).
//
// Evaluations are append-only: prior runs' rows and alerts are retained, giving
// a versioned history of how late data changed baselines.
func evaluateAnomalies(ctx context.Context, q *dbgen.Queries, accountIDs []string, from, to time.Time, runID string) error {
	if len(accountIDs) == 0 || from.After(to) {
		return nil
	}
	from, to = truncDate(from), truncDate(to)

	createdRows, err := q.AccountCreatedDates(ctx, accountIDs)
	if err != nil {
		return err
	}
	created := make(map[string]time.Time, len(createdRows))
	for _, e := range createdRows {
		created[e.AccountID] = truncDate(e.CreatedDate)
	}

	// Daily totals from 30 days before `from` (needed for baselines of the
	// first targets) through `to`. Missing days are zero-filled.
	daily, err := q.DailyAccountTotalsInRange(ctx, dbgen.DailyAccountTotalsInRangeParams{
		CostDate:   addDays(from, -stats.WindowDays),
		CostDate_2: to,
	})
	if err != nil {
		return err
	}
	type ac struct{ a, c string }
	series := map[ac]map[time.Time]decimal.Decimal{}
	currencies := map[string]map[string]bool{} // account -> set of currencies
	for _, d := range daily {
		key := ac{d.AccountID, d.Currency}
		if series[key] == nil {
			series[key] = map[time.Time]decimal.Decimal{}
		}
		day := truncDate(d.CostDate)
		series[key][day] = d.Total
		if currencies[d.AccountID] == nil {
			currencies[d.AccountID] = map[string]bool{}
		}
		currencies[d.AccountID][d.Currency] = true
	}

	// Evaluate every calendar day in [from, to] for every (account,currency)
	// the account ever used. Days without a total are zero-spend days.
	params := dbgen.InsertAnomalyEvaluationParams{
		Column1: []string{}, Column2: []string{}, Column3: []string{},
		Column4: []string{}, Column5: []string{}, Column6: []string{},
		Column7: []string{}, Column8: []string{}, Column9: []string{},
	}
	for _, aid := range accountIDs {
		for _, cur := range mapKeys(currencies[aid]) {
			key := ac{aid, cur}
			for day := from; !day.After(to); day = day.AddDate(0, 0, 1) {
				actual, ok := series[key][day]
				if !ok {
					actual = decimal.Zero
				}
				res := stats.Evaluate(series[key], created[aid], day, actual)

				var mean, std, thr *decimal.Decimal
				if res.Status != stats.StatusInsufficientHistory {
					m, s, th := res.Mean, res.Std, res.Threshold
					mean, std, thr = &m, &s, &th
				}
				params.Column1 = append(params.Column1, runID)
				params.Column2 = append(params.Column2, aid)
				params.Column3 = append(params.Column3, cur)
				params.Column4 = append(params.Column4, dateStr(day))
				params.Column5 = append(params.Column5, actual.String())
				params.Column6 = append(params.Column6, decimalStr(mean))
				params.Column7 = append(params.Column7, decimalStr(std))
				params.Column8 = append(params.Column8, decimalStr(thr))
				params.Column9 = append(params.Column9, string(res.Status))
			}
		}
	}
	if len(params.Column9) == 0 {
		return nil
	}
	return q.InsertAnomalyEvaluation(ctx, params)
}

// decimalStr renders NULL for a nil pointer so UNNEST(...)::numeric yields null.
func decimalStr(d *decimal.Decimal) string {
	if d == nil {
		return ""
	}
	return d.String()
}
