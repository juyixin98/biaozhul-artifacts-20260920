package service

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"

	"github.com/clearsettle/clearsettle/internal/db"
	"github.com/clearsettle/clearsettle/internal/domain"
)

// DiscrepancyThresholdCents: per-statement and balance differences at or
// below 5 cents are accepted as rounding noise; anything larger is recorded.
const DiscrepancyThresholdCents int64 = 5

// ReconResult is returned for a (merchant, date) reconciliation run.
type ReconResult struct {
	Run            db.ReconciliationRun `json:"run"`
	Discrepancies  []db.Discrepancy     `json:"discrepancies"`
	AlreadyExisted bool                 `json:"already_existed"`
}

// StatementInput is an admin-provided gateway statement row.
type StatementInput struct {
	RefID       uuid.UUID `json:"ref_id"`
	Kind        string    `json:"kind"` // capture | refund
	AmountCents int64     `json:"amount_cents"`
	StmtDate    time.Time `json:"stmt_date"`
}

// ImportStatementRow records or overrides one simulated gateway statement row.
func (s *Service) ImportStatementRow(ctx context.Context, actor Actor,
	merchantID uuid.UUID, in StatementInput) error {
	if actor.Role != "admin" {
		return domain.ErrForbidden
	}
	if in.Kind != "capture" && in.Kind != "refund" {
		return fmt.Errorf("%w: kind must be capture or refund", domain.ErrValidation)
	}
	if in.AmountCents <= 0 {
		return domain.ErrAmountInvalid
	}
	return s.inTx(ctx, func(q *db.Queries) error {
		if _, err := q.GetMerchant(ctx, merchantID); err != nil {
			if errors.Is(err, pgx.ErrNoRows) {
				return domain.ErrNotFound
			}
			return err
		}
		if _, err := q.UpsertGatewayStatement(ctx, db.UpsertGatewayStatementParams{
			MerchantID:  merchantID,
			RefID:       in.RefID,
			Kind:        in.Kind,
			AmountCents: in.AmountCents,
			StmtDate:    truncateDate(in.StmtDate),
		}); err != nil {
			return err
		}
		return audit(ctx, q, actor, &merchantID, "gateway.statement.import", "gateway_statement",
			in.RefID.String(), []byte(fmt.Sprintf(`{"kind":%q,"amount":%d}`, in.Kind, in.AmountCents)))
	})
}

// SyncGatewayStatements regenerates the simulated gateway's statement rows
// from our own captures/refunds. This models a perfect acquirer feed; import
// StatementInput rows afterwards to introduce discrepancies.
func (s *Service) SyncGatewayStatements(ctx context.Context, actor Actor,
	merchantID uuid.UUID, asOf time.Time) (int, error) {
	if actor.Role != "admin" {
		return 0, domain.ErrForbidden
	}
	cutoff := truncateDate(asOf).Add(24 * time.Hour)
	n := 0
	err := s.inTx(ctx, func(q *db.Queries) error {
		payments, err := q.ListPaymentsCreatedBefore(ctx,
			db.ListPaymentsCreatedBeforeParams{
				MerchantID: merchantID,
				CreatedAt:  pgtype.Timestamptz{Time: cutoff, Valid: true},
			})
		if err != nil {
			return err
		}
		for _, p := range payments {
			if p.CapturedCents <= 0 {
				continue
			}
			if _, err := q.UpsertGatewayStatement(ctx, db.UpsertGatewayStatementParams{
				MerchantID:  merchantID,
				RefID:       p.ID,
				Kind:        "capture",
				AmountCents: p.CapturedCents,
				StmtDate:    truncateDate(p.CapturedAt.Time),
			}); err != nil {
				return err
			}
			n++
		}
		refunds, err := q.ListRefundsCreatedBefore(ctx,
			db.ListRefundsCreatedBeforeParams{
				MerchantID: merchantID,
				CreatedAt:  pgtype.Timestamptz{Time: cutoff, Valid: true},
			})
		if err != nil {
			return err
		}
		for _, r := range refunds {
			if _, err := q.UpsertGatewayStatement(ctx, db.UpsertGatewayStatementParams{
				MerchantID:  merchantID,
				RefID:       r.ID,
				Kind:        "refund",
				AmountCents: r.AmountCents,
				StmtDate:    truncateDate(r.CreatedAt.Time),
			}); err != nil {
				return err
			}
			n++
		}
		return audit(ctx, q, actor, &merchantID, "gateway.statement.sync", "merchant",
			merchantID.String(), []byte(fmt.Sprintf(`{"rows":%d}`, n)))
	})
	return n, err
}

// Reconcile runs the daily reconciliation for a merchant.
//
// It is idempotent and crash-resumable: one run exists per
// (merchant, run_date). A finished run is returned unchanged; a run left in
// 'running' by a killed process is continued (its discrepancy rows are
// rewritten), so interrupted re-runs never duplicate discrepancies.
func (s *Service) Reconcile(ctx context.Context, actor Actor,
	merchantID uuid.UUID, runDate time.Time) (ReconResult, error) {
	if actor.Role != "admin" {
		return ReconResult{}, domain.ErrForbidden
	}
	date := truncateDate(runDate)
	cutoff := date.Add(24 * time.Hour)

	// Ensure the day is settled first — reconciliation compares settled state.
	if _, err := s.SettleDay(ctx, actor, merchantID, date); err != nil {
		return ReconResult{}, err
	}

	var run db.ReconciliationRun
	err := s.inTx(ctx, func(q *db.Queries) error {
		existing, gerr := q.GetReconRun(ctx, db.GetReconRunParams{
			MerchantID: merchantID, RunDate: date,
		})
		if gerr == nil && existing.Status == "done" {
			run = existing
			return nil
		}
		if gerr == nil {
			run = existing // 'running' left by a crash: reuse
			return nil
		}
		if !errors.Is(gerr, pgx.ErrNoRows) {
			return gerr
		}
		run, gerr = q.CreateReconRun(ctx, db.CreateReconRunParams{
			MerchantID: merchantID, RunDate: date,
		})
		if gerr != nil {
			// Race with another runner: fetch the row it inserted.
			existing, err2 := q.GetReconRun(ctx, db.GetReconRunParams{
				MerchantID: merchantID, RunDate: date,
			})
			if err2 != nil {
				return gerr
			}
			run = existing
		}
		return nil
	})
	if err != nil {
		return ReconResult{}, err
	}
	if run.Status == "done" {
		disc, err := s.q.ListDiscrepancies(ctx, run.ID)
		if err != nil {
			return ReconResult{}, err
		}
		return ReconResult{Run: run, Discrepancies: disc, AlreadyExisted: true}, nil
	}

	var disc []db.Discrepancy
	err = s.retryTx(ctx, func(q *db.Queries) error {
		locked, err := q.GetReconRunForUpdate(ctx,
			db.GetReconRunForUpdateParams{MerchantID: merchantID, RunDate: date})
		if err != nil {
			return err
		}
		if locked.Status == "done" {
			run = locked
			return nil
		}
		run = locked
		if err := q.DeleteDiscrepanciesForRun(ctx, run.ID); err != nil {
			return err
		}
		disc = nil

		payments, err := q.ListPaymentsCreatedBefore(ctx,
			db.ListPaymentsCreatedBeforeParams{
				MerchantID: merchantID,
				CreatedAt:  pgtype.Timestamptz{Time: cutoff, Valid: true},
			})
		if err != nil {
			return err
		}
		refunds, err := q.ListRefundsCreatedBefore(ctx,
			db.ListRefundsCreatedBeforeParams{
				MerchantID: merchantID,
				CreatedAt:  pgtype.Timestamptz{Time: cutoff, Valid: true},
			})
		if err != nil {
			return err
		}
		stmts, err := q.ListAllGatewayStatements(ctx, merchantID)
		if err != nil {
			return err
		}
		// Compare every statement whose settlement would have arrived by the
		// run-date cutoff (statements for run_date itself are included).
		stmtByKey := make(map[stmtKey]db.GatewayStatement)
		for _, st := range stmts {
			if !st.StmtDate.Before(cutoff) {
				continue
			}
			stmtByKey[stmtKey{st.RefID, st.Kind}] = st
		}

		var capturedTotal, refundedTotal, feeTotal, feeRefundedTotal int64
		seenStmt := make(map[stmtKey]bool)

		addDisc := func(kind string, txID *uuid.UUID, expected, actual, diff int64, detail string) error {
			if diff < 0 {
				diff = -diff
			}
			if diff <= DiscrepancyThresholdCents {
				return nil
			}
			var ref uuid.UUID
			if txID != nil {
				ref = *txID
			}
			d, err := q.InsertDiscrepancy(ctx, db.InsertDiscrepancyParams{
				RunID:         run.ID,
				Kind:          kind,
				TxID:          &ref,
				ExpectedCents: expected,
				ActualCents:   actual,
				DiffCents:     diff,
				Detail:        detail,
			})
			if err != nil {
				return err
			}
			disc = append(disc, d)
			return nil
		}

		for i := range payments {
			p := payments[i]
			if p.CapturedCents <= 0 {
				continue
			}
			capturedTotal += p.CapturedCents
			refundedTotal += p.RefundedCents
			feeTotal += p.FeeCents
			feeRefundedTotal += p.RefundedFeeCents

			st, ok := stmtByKey[stmtKey{p.ID, "capture"}]
			pid := p.ID
			if !ok {
				if err := addDisc("missing_statement", &pid, p.CapturedCents, 0,
					p.CapturedCents, "capture missing from gateway statement"); err != nil {
					return err
				}
			} else if st.AmountCents != p.CapturedCents {
				if err := addDisc("amount_mismatch", &pid, p.CapturedCents, st.AmountCents,
					p.CapturedCents-st.AmountCents, "capture amount differs from statement"); err != nil {
					return err
				}
			}
			seenStmt[stmtKey{p.ID, "capture"}] = true
		}

		for i := range refunds {
			r := refunds[i]
			st, ok := stmtByKey[stmtKey{r.ID, "refund"}]
			rid := r.ID
			if !ok {
				if err := addDisc("missing_statement", &rid, r.AmountCents, 0,
					r.AmountCents, "refund missing from gateway statement"); err != nil {
					return err
				}
			} else if st.AmountCents != r.AmountCents {
				if err := addDisc("amount_mismatch", &rid, r.AmountCents, st.AmountCents,
					r.AmountCents-st.AmountCents, "refund amount differs from statement"); err != nil {
					return err
				}
			}
			seenStmt[stmtKey{r.ID, "refund"}] = true
		}

		// Statement rows with no matching internal record.
		for _, st := range stmtByKey {
			k := stmtKey{st.RefID, st.Kind}
			if seenStmt[k] {
				continue
			}
			ref := st.RefID
			kind := "missing_payment"
			if st.Kind == "refund" {
				kind = "missing_refund"
			}
			if err := addDisc(kind, &ref, 0, st.AmountCents, st.AmountCents,
				"gateway statement row has no internal record"); err != nil {
				return err
			}
		}

		// Ledger sanity checks on the as-of snapshot. Accounts are
		// per-merchant, so the identity holds independently of other
		// merchants.
		expectedCash := capturedTotal - refundedTotal
		cashAcct, err := q.GetAccount(ctx, db.GetAccountParams{MerchantID: &merchantID, Code: AcctGatewayCash})
		if err != nil {
			return err
		}
		payableAcct, err := q.GetAccount(ctx, db.GetAccountParams{MerchantID: &merchantID, Code: AcctPayable})
		if err != nil {
			return err
		}
		feeAcct, err := q.GetAccount(ctx, db.GetAccountParams{MerchantID: &merchantID, Code: AcctFeeRevenue})
		if err != nil {
			return err
		}
		cutoffTS := pgtype.Timestamptz{Time: cutoff, Valid: true}
		gatewayBalance, err := q.SumAccountEntriesBefore(ctx,
			db.SumAccountEntriesBeforeParams{AccountID: cashAcct.ID, CreatedAt: cutoffTS})
		if err != nil {
			return err
		}
		payableBalance, err := q.SumAccountEntriesBefore(ctx,
			db.SumAccountEntriesBeforeParams{AccountID: payableAcct.ID, CreatedAt: cutoffTS})
		if err != nil {
			return err
		}
		feeBalance, err := q.SumAccountEntriesBefore(ctx,
			db.SumAccountEntriesBeforeParams{AccountID: feeAcct.ID, CreatedAt: cutoffTS})
		if err != nil {
			return err
		}
		// Double-entry identity across the three money accounts: every balanced
		// posting moves gateway_cash + fee_revenue + payable by zero in total,
		// so the account sum must be zero (tolerated by the 5c threshold).
		ledgerDiff := gatewayBalance + feeBalance + payableBalance
		if err := addDisc("ledger_balance", nil, 0, ledgerDiff, ledgerDiff,
			"gateway_cash + fee_revenue + payable must net to zero"); err != nil {
			return err
		}

		now := pgtype.Timestamptz{Time: s.clock.Now(), Valid: true}
		var settledTotal int64
		var batchID *uuid.UUID
		batch, berr := q.GetBatchByMerchantDate(ctx,
			db.GetBatchByMerchantDateParams{MerchantID: merchantID, BatchDate: date})
		if berr == nil {
			settledTotal = batch.TotalCents
			bid := batch.ID
			batchID = &bid
		}
		run, err = q.CompleteReconRun(ctx, db.CompleteReconRunParams{
			ID:                 run.ID,
			BatchID:            batchID,
			CapturedCents:      capturedTotal,
			RefundedCents:      refundedTotal,
			FeesCents:          feeTotal - feeRefundedTotal,
			SettledCents:       settledTotal,
			GatewayCashCents:   gatewayBalance,
			PayableCents:       payableBalance,
			ExpectedCashCents:  expectedCash,
			DiffCents:          ledgerDiff,
			DiscrepanciesCount: int32(len(disc)),
			CompletedAt:        now,
		})
		if err != nil {
			return err
		}
		return audit(ctx, q, actor, &merchantID, "reconciliation.complete", "reconciliation_run",
			run.ID.String(), []byte(fmt.Sprintf(`{"discrepancies":%d}`, len(disc))))
	})
	if err != nil {
		return ReconResult{}, err
	}
	return ReconResult{Run: run, Discrepancies: disc}, nil
}

type stmtKey struct {
	RefID uuid.UUID
	Kind  string
}

// ListReconRuns returns a merchant's reconciliation runs.
func (s *Service) ListReconRuns(ctx context.Context, actor Actor, merchantID uuid.UUID) ([]db.ReconciliationRun, error) {
	if err := actor.requireMerchant(merchantID); err != nil {
		return nil, err
	}
	return s.q.ListReconRuns(ctx, merchantID)
}
