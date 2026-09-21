package service

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgtype"

	"github.com/clearsettle/clearsettle/internal/db"
	"github.com/clearsettle/clearsettle/internal/domain"
)

// SettlementResult is the outcome of (resuming) a day's settlement run.
type SettlementResult struct {
	Batch          db.SettlementBatch `json:"batch"`
	ItemsSettled   int                `json:"items_settled"`
	AlreadyExisted bool               `json:"already_existed"` // batch was already complete
}

// SettleHook is invoked after each payment's settlement item commits. Tests
// use it to kill the process (context cancel) mid-batch to prove the run is
// resumable without double posting.
type SettleHook func(paymentID uuid.UUID)

// settleHook is nil in production; tests set it on the Service.
var _ SettleHook

// SettleDay settles all of a merchant's captured (and pre-settlement
// refunded) payments created before the end of batchDate into one batch.
//
// It is idempotent and crash-safe:
//   - there is one batch per (merchant, date); a completed batch is returned
//     unchanged;
//   - each payment is settled in its own transaction with a UNIQUE
//     settlement_items(payment_id) guard, so a crash (or duplicate runner)
//     never posts a payment twice — the next run resumes with the remaining
//     payments;
//   - the batch is marked done only after every eligible payment is committed.
func (s *Service) SettleDay(ctx context.Context, actor Actor, merchantID uuid.UUID,
	batchDate time.Time) (SettlementResult, error) {
	if actor.Role != "admin" {
		return SettlementResult{}, domain.ErrForbidden
	}
	date := truncateDate(batchDate)
	cutoff := date.Add(24 * time.Hour) // payments created on/before batchDate

	// Create or resume the batch.
	var batch db.SettlementBatch
	err := s.inTx(ctx, func(q *db.Queries) error {
		existing, err := q.GetBatchByMerchantDateForUpdate(ctx,
			db.GetBatchByMerchantDateForUpdateParams{MerchantID: merchantID, BatchDate: date})
		if err == nil {
			batch = existing
			return nil
		}
		if !errors.Is(err, pgx.ErrNoRows) {
			return err
		}
		batch, err = q.CreateBatch(ctx, db.CreateBatchParams{
			MerchantID: merchantID, BatchDate: date,
		})
		if err != nil {
			var pgErr *pgconn.PgError
			if errors.As(err, &pgErr) && pgErr.Code == "23505" {
				// Concurrent first run created it; fetch and resume.
				batch, err = q.GetBatchByMerchantDateForUpdate(ctx,
					db.GetBatchByMerchantDateForUpdateParams{MerchantID: merchantID, BatchDate: date})
				return err
			}
			return err
		}
		return audit(ctx, q, actor, &merchantID, "settlement.start", "settlement_batch",
			batch.ID.String(), []byte(fmt.Sprintf(`{"date":%q}`, date.Format("2006-01-02"))))
	})
	if err != nil {
		return SettlementResult{}, err
	}

	if batch.Status == "done" {
		return SettlementResult{Batch: batch, AlreadyExisted: true}, nil
	}

	settled := 0
	for {
		if err := ctx.Err(); err != nil {
			return SettlementResult{}, err
		}
		done, id, err := s.settleOne(ctx, actor, batch.ID, merchantID, cutoff)
		if err != nil {
			return SettlementResult{}, err
		}
		if done {
			break
		}
		settled++
		if s.hook != nil {
			s.hook(id)
		}
		if s.cancelFn != nil {
			s.cancelFn()
		}
	}

	// Finalize the batch from the committed items.
	err = s.retryTx(ctx, func(q *db.Queries) error {
		sums, err := q.SumBatchItems(ctx, batch.ID)
		if err != nil {
			return err
		}
		locked, err := q.GetBatchByMerchantDateForUpdate(ctx,
			db.GetBatchByMerchantDateForUpdateParams{MerchantID: merchantID, BatchDate: date})
		if err != nil {
			return err
		}
		if locked.Status == "done" {
			batch = locked
			return nil
		}
		now := pgtype.Timestamptz{Time: s.clock.Now(), Valid: true}
		batch, err = q.MarkBatchDone(ctx, db.MarkBatchDoneParams{
			ID:          batch.ID,
			TotalCents:  sums.Gross,
			FeeCents:    sums.Fee,
			NetCents:    sums.Net,
			CompletedAt: now,
		})
		if err != nil {
			return err
		}
		return audit(ctx, q, actor, &merchantID, "settlement.complete", "settlement_batch",
			batch.ID.String(), []byte(fmt.Sprintf(`{"net":%d}`, sums.Net)))
	})
	if err != nil {
		return SettlementResult{}, err
	}
	return SettlementResult{Batch: batch, ItemsSettled: settled}, nil
}

// settleOne settles a single not-yet-settled eligible payment in its own
// transaction. done == true means no eligible payments remain.
func (s *Service) settleOne(ctx context.Context, actor Actor,
	batchID, merchantID uuid.UUID, cutoff time.Time) (done bool, id uuid.UUID, err error) {
	// Per-payment transaction at READ COMMITTED: ListSettleablePayments'
	// FOR UPDATE SKIP LOCKED claims rows without serializable read-write
	// conflicts, and settlement_items(payment_id) unique is the duplicate guard.
	err = s.inTxReadCommitted(ctx, func(q *db.Queries) error {
		payments, err := q.ListSettleablePayments(ctx,
			db.ListSettleablePaymentsParams{
				MerchantID: merchantID,
				CreatedAt:  pgtype.Timestamptz{Time: cutoff, Valid: true},
			})
		if err != nil {
			return err
		}
		var next *db.Transaction
		for i := range payments {
			already, err := q.PaymentSettledInBatch(ctx, payments[i].ID)
			if err != nil {
				return err
			}
			if !already {
				next = &payments[i]
				break
			}
		}
		if next == nil {
			done = true
			return nil
		}
		p := next
		id = p.ID

		// Fee the platform keeps after pre-settlement fee releases.
		feeKept := p.FeeCents - p.RefundedFeeCents
		gross := p.CapturedCents - p.RefundedCents // remaining captured cash
		net := gross - feeKept                     // may be negative on a fully-refunded payment

		if err := q.AddBatchItem(ctx, db.AddBatchItemParams{
			BatchID:    batchID,
			PaymentID:  p.ID,
			GrossCents: gross,
			FeeCents:   feeKept,
			NetCents:   net,
		}); err != nil {
			return err
		}
		now := s.clock.Now()
		if _, err := q.MarkPaymentSettled(ctx, db.MarkPaymentSettledParams{
			ID:        p.ID,
			SettledAt: pgtype.Timestamptz{Time: now, Valid: true},
		}); err != nil {
			return err
		}
		// Pay out net cash to the merchant: discharge the payable liability
		// (+net) and move cash out of gateway (-net). Negative net (clawback
		// from a fully/over-refunded payment) posts the reverse.
		legs := []Posting{
			{merchantAccount(merchantID, AcctPayable), net},
			{merchantAccount(merchantID, AcctGatewayCash), -net},
		}
		if err := postEntries(ctx, q, s.clock.Now(), "settlement", "batch", batchID, legs); err != nil {
			return err
		}
		return nil
	})
	return done, id, err
}

func truncateDate(t time.Time) time.Time {
	t = t.UTC()
	y, m, d := t.Date()
	return time.Date(y, m, d, 0, 0, 0, 0, time.UTC)
}

// GetBatch fetches a batch if the actor may see it.
func (s *Service) GetBatch(ctx context.Context, actor Actor, id uuid.UUID) (db.SettlementBatch, []db.SettlementItem, error) {
	b, err := s.q.GetBatch(ctx, id)
	if errors.Is(err, pgx.ErrNoRows) {
		return db.SettlementBatch{}, nil, domain.ErrNotFound
	}
	if err != nil {
		return db.SettlementBatch{}, nil, err
	}
	if err := actor.requireMerchant(b.MerchantID); err != nil {
		return db.SettlementBatch{}, nil, err
	}
	items, err := s.q.ListBatchItems(ctx, b.ID)
	if err != nil {
		return db.SettlementBatch{}, nil, err
	}
	return b, items, nil
}

// ListBatches lists a merchant's batches.
func (s *Service) ListBatches(ctx context.Context, actor Actor, merchantID uuid.UUID) ([]db.SettlementBatch, error) {
	if err := actor.requireMerchant(merchantID); err != nil {
		return nil, err
	}
	return s.q.ListBatchesByMerchant(ctx, merchantID)
}
