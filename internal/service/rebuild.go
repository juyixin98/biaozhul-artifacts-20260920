package service

import (
	"context"
	"time"

	"costlens/internal/db/dbgen"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"
)

// recomputeSlices replaces all summary rows touched by dates [minD, maxD] for
// the given accounts. Daily slices are deleted by day range; monthly slices are
// deleted for every month the range spans and recomputed from the full month
// span (not just the imported days), so late data landing in an old month is
// incorporated correctly. Different currencies are never mixed: every
// aggregate groups by currency.
func recomputeSlices(ctx context.Context, q *dbgen.Queries, minD, maxD time.Time, accountIDs, ccIDs []string) error {
	if len(accountIDs) == 0 {
		return nil
	}
	if err := q.DeleteDailyAccountSlice(ctx, dbgen.DeleteDailyAccountSliceParams{
		CostDate: minD, CostDate_2: maxD, Column3: accountIDs,
	}); err != nil {
		return err
	}
	if err := q.DeleteDailyCostCenterSlice(ctx, dbgen.DeleteDailyCostCenterSliceParams{
		CostDate: minD, CostDate_2: maxD, Column3: ccIDs,
	}); err != nil {
		return err
	}

	monthLo, monthHi := monthStart(minD), monthStart(maxD)
	if err := q.DeleteMonthlyAccountSlice(ctx, dbgen.DeleteMonthlyAccountSliceParams{
		Column1: monthsBetween(monthLo, monthHi), Column2: accountIDs,
	}); err != nil {
		return err
	}
	if err := q.DeleteMonthlyCostCenterSlice(ctx, dbgen.DeleteMonthlyCostCenterSliceParams{
		Column1: monthsBetween(monthLo, monthHi), Column2: ccIDs,
	}); err != nil {
		return err
	}

	// Recompute from the full month span so every affected month is complete.
	recomputeLo, recomputeHi := monthLo, monthEnd(monthHi)
	params := dbgen.RecomputeDailyAccountParams{
		CostDate: minD, CostDate_2: maxD, Column3: accountIDs,
	}
	if err := q.RecomputeDailyAccount(ctx, params); err != nil {
		return err
	}
	if err := q.RecomputeDailyCostCenter(ctx, dbgen.RecomputeDailyCostCenterParams{
		CostDate: minD, CostDate_2: maxD, Column3: accountIDs,
	}); err != nil {
		return err
	}
	if err := q.RecomputeMonthlyAccount(ctx, dbgen.RecomputeMonthlyAccountParams{
		CostDate: recomputeLo, CostDate_2: recomputeHi, Column3: accountIDs,
	}); err != nil {
		return err
	}
	if err := q.RecomputeMonthlyCostCenter(ctx, dbgen.RecomputeMonthlyCostCenterParams{
		CostDate: recomputeLo, CostDate_2: recomputeHi, Column3: accountIDs,
	}); err != nil {
		return err
	}
	return nil
}

// Rebuild truncates and recomputes every summary from raw costs, then runs
// budget and anomaly evaluation over all data. It takes the same advisory lock
// as imports, so rows imported during the rebuild cannot be lost or counted
// twice: the import waits for rebuild commit and then recomputes incrementally
// on the rebuilt state.
func (s *Service) Rebuild(ctx context.Context) (int, error) {
	rebuilt := 0
	err := s.transact(ctx, func(tx pgx.Tx) error {
		q := dbgen.New(tx)
		if err := q.AcquireTxAdvisoryLock(ctx, advisoryLockKey); err != nil {
			return err
		}

		if err := q.TruncateSummaries(ctx); err != nil {
			return err
		}

		minD, err := q.MinCostDate(ctx)
		if err != nil {
			return err
		}
		maxD, err := q.MaxCostDate(ctx)
		if err != nil {
			return err
		}
		if minD.IsZero() {
			return nil // no data
		}
		minD, maxD = truncDate(minD), truncDate(maxD)

		allAccounts, err := q.AllAccountIDs(ctx)
		if err != nil {
			return err
		}
		accountIDs := allAccounts

		// All cost centers: recompute joins resolve them from accounts.
		lo, hi := monthStart(minD), monthEnd(monthStart(maxD))
		if err := q.RecomputeDailyAccount(ctx, dbgen.RecomputeDailyAccountParams{
			CostDate: minD, CostDate_2: maxD, Column3: accountIDs,
		}); err != nil {
			return err
		}
		if err := q.RecomputeDailyCostCenter(ctx, dbgen.RecomputeDailyCostCenterParams{
			CostDate: minD, CostDate_2: maxD, Column3: accountIDs,
		}); err != nil {
			return err
		}
		if err := q.RecomputeMonthlyAccount(ctx, dbgen.RecomputeMonthlyAccountParams{
			CostDate: lo, CostDate_2: hi, Column3: accountIDs,
		}); err != nil {
			return err
		}
		if err := q.RecomputeMonthlyCostCenter(ctx, dbgen.RecomputeMonthlyCostCenterParams{
			CostDate: lo, CostDate_2: hi, Column3: accountIDs,
		}); err != nil {
			return err
		}
		rebuilt = len(accountIDs)

		run, err := q.CreateAnomalyRun(ctx, dbgen.CreateAnomalyRunParams{
			Kind: "rebuild", ImportID: pgtype.Text{Valid: false},
		})
		if err != nil {
			return err
		}

		// Budget evaluation for every current budget version.
		if err := evaluateAllBudgets(ctx, q, run.ID); err != nil {
			return err
		}

		// Anomaly evaluation for every observed day. Accounts created after a
		// window's start are marked insufficient_history; existing accounts
		// with zero-spend days use zeros in their baseline.
		if err := evaluateAnomalies(ctx, q, accountIDs, minD, maxD, run.ID); err != nil {
			return err
		}
		return nil
	})
	return rebuilt, err
}
