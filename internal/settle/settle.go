// Package settle runs merchant daily settlement. It is crash-safe:
//
//   - exactly one settlement_batches row per (merchant, UTC date)
//   - all payments of that day are locked FOR UPDATE SKIP LOCKED before posting
//   - the batch row, payment updates and ledger posting commit in one tx
//
// A crash after commit is detected by the unique (merchant, batch_date) row;
// the worker skips completed batches. A crash mid-transaction leaves a
// 'processing' row that the next run takes over (row lock + advisory lock).
package settle

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/clearsettle/clearsettle/internal/audit"
	"github.com/clearsettle/clearsettle/internal/ledger"
	"github.com/clearsettle/clearsettle/internal/store"
)

type Service struct {
	pool    *pgxpool.Pool
	q       *store.Queries
	lockTag string
}

func New(pool *pgxpool.Pool, lockTag string) *Service {
	return &Service{pool: pool, q: store.New(pool), lockTag: lockTag}
}

// Cutoff returns the latest business date eligible for settlement: captures
// strictly before now - horizon are considered final.
func Cutoff(now time.Time, horizon time.Duration) time.Time {
	return now.UTC().Add(-horizon)
}

// DayResult summarises one (merchant, day) settlement.
type DayResult struct {
	BatchID       uuid.UUID
	MerchantID    uuid.UUID
	Date          time.Time
	Status        string
	GrossCaptured int64
	TotalFees     int64
	TotalRefunds  int64
	RefundedFees  int64
	NetAmount     int64
	PaymentCount  int32
	Skipped       bool // already completed
}

// SettleDay settles a single merchant/day. Safe to call repeatedly.
func (s *Service) SettleDay(ctx context.Context, merchantID uuid.UUID, day time.Time, actor audit.Entry) (DayResult, error) {
	day = day.UTC().Truncate(24 * time.Hour)
	res := DayResult{MerchantID: merchantID, Date: day}

	err := pgx.BeginTxFunc(ctx, s.pool, pgx.TxOptions{}, func(tx pgx.Tx) error {
		q := store.New(tx)

		// Advisory lock serializes two workers that pick the same pair even
		// before the unique row exists. Key derived exactly as documented in
		// migration 0006: hashtext('settle:<merchant>:<date>').
		key := fmt.Sprintf("settle:%s:%s", merchantID, day.Format("2006-01-02"))

		var lockKey int64
		if err := tx.QueryRow(ctx, `SELECT hashtext($1)::bigint`, key).Scan(&lockKey); err != nil {
			return err
		}
		if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock($1)`, lockKey); err != nil {
			return err
		}

		// Fast path: already completed.
		existing, err := q.GetSettlementBatchForUpdate(ctx,
			store.GetSettlementBatchForUpdateParams{MerchantID: merchantID, BatchDate: dateArg(day)})
		if err == nil && existing.Status == "completed" {
			res.BatchID = existing.ID
			res.Status = "completed"
			res.GrossCaptured = existing.GrossCaptured
			res.TotalFees = existing.TotalFees
			res.TotalRefunds = existing.TotalRefunds
			res.RefundedFees = existing.RefundedFees
			res.NetAmount = existing.NetAmount
			res.PaymentCount = existing.PaymentCount
			res.Skipped = true
			return nil
		} else if err != nil && !errors.Is(err, pgx.ErrNoRows) {
			return err
		}

		agg, err := q.AggregatePaymentsForBatchDay(ctx, store.AggregatePaymentsForBatchDayParams{
			MerchantID: merchantID,
			BatchDate:  dateArg(day),
		})
		if err != nil {
			return err
		}
		if agg.PaymentCount == 0 {
			// Nothing to do; don't create a batch row for empty days.
			res.Skipped = true
			res.Status = "empty"
			return nil
		}

		batch, err := q.UpsertSettlementBatch(ctx, store.UpsertSettlementBatchParams{
			MerchantID: merchantID, BatchDate: dateArg(day), LockedBy: nullable(s.lockTag),
		})
		// On takeover of a crashed 'processing' row, ON CONFLICT DO NOTHING
		// returns no row; reload it instead.
		var zero uuid.UUID
		if errors.Is(err, pgx.ErrNoRows) || (err == nil && batch.ID == zero) {
			batch, err = q.GetSettlementBatchForUpdate(ctx,
				store.GetSettlementBatchForUpdateParams{MerchantID: merchantID, BatchDate: dateArg(day)})
			if err != nil {
				return err
			}
			if batch.Status == "completed" {
				res.BatchID = batch.ID
				res.Skipped = true
				res.Status = "completed"
				return nil
			}
		} else if err != nil {
			return err
		}
		res.BatchID = batch.ID

		// Lock the day's payments; SKIP LOCKED rows can't exist here because the
		// advisory lock is ours, but keep the clause for defense in depth.
		rows, err := q.PaymentsForBatchDay(ctx, store.PaymentsForBatchDayParams{
			MerchantID: merchantID, BatchDate: dateArg(day), RowLimit: 100000,
		})
		if err != nil {
			return err
		}
		if len(rows) != int(agg.PaymentCount) {
			return fmt.Errorf("settlement race: locked %d payments but aggregate saw %d", len(rows), agg.PaymentCount)
		}

		if err := q.MarkBatchPaymentsSettled(ctx, store.MarkBatchPaymentsSettledParams{
			MerchantID: merchantID, BatchDate: dateArg(day), BatchID: &batch.ID,
		}); err != nil {
			return err
		}

		// Payout posting: move the net payable off the merchant liability into
		// cash paid out. Settlement is money leaving the platform to the bank.
		accts, err := ledger.LoadAccounts(ctx, q, merchantID)
		if err != nil {
			return err
		}
		if agg.NetPayable != 0 {
			if err := ledger.Post(ctx, tx, q, ledger.Posting{
				RefType: ledger.RefTypeSettlement, RefID: batch.ID,
				Lines: []ledger.Line{
					// Liabilities down by net payout: debit payable (positive in
					// our sign scheme means balance reduction here — see README).
					{AccountID: accts.Payable.ID, Amount: agg.NetPayable},
					{AccountID: accts.Cash.ID, Amount: -agg.NetPayable},
				},
			}); err != nil {
				return err
			}
		}

		completed, err := q.CompleteSettlementBatch(ctx, store.CompleteSettlementBatchParams{
			ID: batch.ID, MerchantID: merchantID,
			GrossCaptured: agg.GrossCaptured,
			TotalFees:     agg.TotalFees,
			TotalRefunds:  agg.TotalRefunds,
			RefundedFees:  agg.RefundedFees,
			NetAmount:     agg.NetPayable,
			PaymentCount:  agg.PaymentCount,
		})
		if err != nil {
			return err
		}
		detail := map[string]any{
			"gross_captured": agg.GrossCaptured, "fees": agg.TotalFees,
			"refunds": agg.TotalRefunds, "refunded_fees": agg.RefundedFees,
			"net": agg.NetPayable, "payments": agg.PaymentCount,
		}
		actor.MerchantID = &merchantID
		actor.Action = "settlement.complete"
		actor.TargetType = "settlement_batch"
		actor.TargetID = batch.ID.String()
		actor.Detail = detail
		if err := audit.Write(ctx, q, actor); err != nil {
			return err
		}
		res.Status = "completed"
		res.GrossCaptured = completed.GrossCaptured
		res.TotalFees = completed.TotalFees
		res.TotalRefunds = completed.TotalRefunds
		res.RefundedFees = completed.RefundedFees
		res.NetAmount = completed.NetAmount
		res.PaymentCount = completed.PaymentCount
		return nil
	})
	return res, err
}

// RunSweep settles every eligible (merchant, day) with captures older than the
// horizon. If scopeMerchant is non-nil, only that merchant's days are touched.
// Returns per-day results and is fully resumable.
func (s *Service) RunSweep(ctx context.Context, now time.Time, horizon time.Duration, scopeMerchant *uuid.UUID, actor audit.Entry) ([]DayResult, error) {
	cutoff := Cutoff(now, horizon)
	pairs, err := s.q.ListMerchantDaysToSettle(ctx, tsArg(cutoff))
	if err != nil {
		return nil, err
	}
	var out []DayResult
	for _, p := range pairs {
		if scopeMerchant != nil && p.MerchantID != *scopeMerchant {
			continue
		}
		// Pair rows from sqlc carry merchant_id and day; select only days fully
		// behind the cutoff (all their captures are older).
		dayEnd := p.Day.Time.Add(24 * time.Hour)
		if !dayEnd.Add(-time.Nanosecond).Before(cutoff) {
			continue
		}
		r, err := s.SettleDay(ctx, p.MerchantID, p.Day.Time, actor)
		if err != nil {
			return out, fmt.Errorf("settle merchant %s day %s: %w", p.MerchantID, p.Day.Time.Format("2006-01-02"), err)
		}
		if !r.Skipped {
			out = append(out, r)
		}
	}
	return out, nil
}

func dateArg(t time.Time) pgtype.Date {
	t = t.UTC()
	return pgtype.Date{Time: time.Date(t.Year(), t.Month(), t.Day(), 0, 0, 0, 0, time.UTC), Valid: true}
}

func tsArg(t time.Time) pgtype.Timestamptz {
	return pgtype.Timestamptz{Time: t.UTC(), Valid: true}
}

func nullable(s string) pgtype.Text {
	if s == "" {
		return pgtype.Text{Valid: false}
	}
	return pgtype.Text{String: s, Valid: true}
}
