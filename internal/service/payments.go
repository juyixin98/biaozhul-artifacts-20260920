package service

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgtype"

	"github.com/clearsettle/clearsettle/internal/db"
	"github.com/clearsettle/clearsettle/internal/domain"
	"github.com/clearsettle/clearsettle/internal/money"
)

// paymentView is the masked JSON representation returned by the API.
type paymentView struct {
	ID               string `json:"id"`
	MerchantID       string `json:"merchant_id"`
	Status           string `json:"status"`
	AmountCents      int64  `json:"amount_cents"`
	CapturedCents    int64  `json:"captured_cents"`
	FeeCents         int64  `json:"fee_cents"`
	RefundedCents    int64  `json:"refunded_cents"`
	RefundedFeeCents int64  `json:"refunded_fee_cents"`
	SettledCents     int64  `json:"settled_cents"`
	Currency         string `json:"currency"`
	Card             string `json:"card"`
	AuthExpiresAt    string `json:"auth_expires_at,omitempty"`
	CapturedAt       string `json:"captured_at,omitempty"`
	RefundDeadline   string `json:"refund_deadline,omitempty"`
	CreatedAt        string `json:"created_at"`
}

func toPaymentView(p db.Transaction) paymentView {
	v := paymentView{
		ID:               p.ID.String(),
		MerchantID:       p.MerchantID.String(),
		Status:           p.Status,
		AmountCents:      p.AmountCents,
		CapturedCents:    p.CapturedCents,
		FeeCents:         p.FeeCents,
		RefundedCents:    p.RefundedCents,
		RefundedFeeCents: p.RefundedFeeCents,
		SettledCents:     p.SettledCents,
		Currency:         p.Currency,
		Card:             MaskCard(p.CardLast4),
		CreatedAt:        p.CreatedAt.Time.Format("2006-01-02T15:04:05Z07:00"),
	}
	if p.AuthExpiresAt.Valid {
		v.AuthExpiresAt = p.AuthExpiresAt.Time.Format("2006-01-02T15:04:05Z07:00")
	}
	if p.CapturedAt.Valid {
		v.CapturedAt = p.CapturedAt.Time.Format("2006-01-02T15:04:05Z07:00")
	}
	if p.RefundDeadline.Valid {
		v.RefundDeadline = p.RefundDeadline.Time.Format("2006-01-02T15:04:05Z07:00")
	}
	return v
}

// AuthorizeInput starts a payment authorization (funds are only held, not
// moved — nothing is posted to the ledger until capture).
type AuthorizeInput struct {
	MerchantID  uuid.UUID
	AmountCents int64  `json:"amount_cents"`
	CardLast4   string `json:"card_last4"`
}

// Authorize creates an authorization. Idempotent on idem.
func (s *Service) Authorize(ctx context.Context, actor Actor, in AuthorizeInput, idem Idem) (db.Transaction, bool, error) {
	if actor.Role != "operator" && actor.Role != "admin" {
		return db.Transaction{}, false, domain.ErrForbidden
	}
	if err := actor.requireMerchant(in.MerchantID); err != nil {
		return db.Transaction{}, false, err
	}
	if in.AmountCents <= 0 {
		return db.Transaction{}, false, domain.ErrAmountInvalid
	}
	if in.CardLast4 == "" {
		return db.Transaction{}, false, fmt.Errorf("%w: card_last4 required", domain.ErrValidation)
	}

	body, replayed, err := s.runIdempotent(ctx, actor, "/v1/payments/authorize", in.MerchantID, idem,
		func(ctx context.Context, q *db.Queries) (actionResult, error) {
			p, err := q.CreatePayment(ctx, db.CreatePaymentParams{
				MerchantID:  in.MerchantID,
				AmountCents: in.AmountCents,
				CardLast4:   in.CardLast4,
				AuthExpiresAt: pgtype.Timestamptz{
					Time: s.clock.Now().Add(domain.AuthWindow), Valid: true,
				},
				CreatedAt: pgtype.Timestamptz{Time: s.clock.Now(), Valid: true},
			})
			if err != nil {
				return actionResult{}, err
			}
			if err := audit(ctx, q, actor, &in.MerchantID, "payment.authorize", "payment",
				p.ID.String(), []byte(fmt.Sprintf(`{"amount":%d}`, in.AmountCents))); err != nil {
				return actionResult{}, err
			}
			return actionResult{resourceID: p.ID, payload: toPaymentView(p)}, nil
		})
	if err != nil {
		return db.Transaction{}, false, err
	}
	// Replays carry the stored payment view; fetch the row for the typed
	// caller (tests).
	rid, perr := extractResourceID(body)
	if perr != nil || rid == uuid.Nil {
		return db.Transaction{}, replayed, perr
	}
	p, gerr := s.q.GetPayment(ctx, rid)
	return p, replayed, gerr
}

// CaptureInput settles an authorization. A partial capture is allowed
// (capture_cents < authorized); it must occur within the 24h auth window.
type CaptureInput struct {
	PaymentID    uuid.UUID
	CaptureCents int64 `json:"capture_cents,omitempty"` // 0 => full authorization
}

// Capture captures an authorization, idempotent on idem.
func (s *Service) Capture(ctx context.Context, actor Actor, in CaptureInput, idem Idem) (db.Transaction, bool, error) {
	if actor.Role != "operator" && actor.Role != "admin" {
		return db.Transaction{}, false, domain.ErrForbidden
	}
	var out db.Transaction

	body, replayed, err := s.runIdempotentTx(ctx, actor, "/v1/payments/capture", idem,
		// scope: resolve merchant without locking (replays must not re-run run).
		func(q *db.Queries) (uuid.UUID, error) {
			p, err := q.GetPayment(ctx, in.PaymentID)
			if errors.Is(err, pgxNoRows) {
				return uuid.Nil, domain.ErrNotFound
			}
			if err != nil {
				return uuid.Nil, err
			}
			if err := actor.requireMerchant(p.MerchantID); err != nil {
				return uuid.Nil, err
			}
			return p.MerchantID, nil
		},
		func(q *db.Queries) error {
			p, err := q.GetPaymentForUpdate(ctx, in.PaymentID)
			if err != nil {
				return err
			}
			if p.Status != domain.StatusAuthorized {
				return fmt.Errorf("%w: payment %s is %s, capture requires authorized",
					domain.ErrInvalidState, p.ID, p.Status)
			}
			if !s.clock.Now().Before(p.AuthExpiresAt.Time) {
				return domain.ErrAuthExpired
			}
			capture := in.CaptureCents
			if capture == 0 {
				capture = p.AmountCents
			}
			if capture <= 0 {
				return domain.ErrAmountInvalid
			}
			if capture > p.AmountCents {
				return domain.ErrCaptureTooLarge
			}

			m, err := q.GetMerchant(ctx, p.MerchantID)
			if err != nil {
				return err
			}
			bps, fixed := merchantFee(m)
			fee := money.Fee(capture, bps, fixed)
			if fee > capture {
				return fmt.Errorf("%w: fee exceeds capture", domain.ErrValidation)
			}

			now := s.clock.Now()
			out, err = q.MarkCaptured(ctx, db.MarkCapturedParams{
				ID:            p.ID,
				CapturedCents: capture,
				FeeCents:      fee,
				CapturedAt:    pgtype.Timestamptz{Time: now, Valid: true},
			})
			if err != nil {
				return err
			}

			// Ledger (simulated), balanced to zero:
			//   gateway_cash +capture          (asset: money received)
			//   payable       -(capture-fee)   (liability: net owed to merchant)
			//   fee_revenue   -fee             (equity contra: fee withheld)
			legs := []Posting{
				{merchantAccount(p.MerchantID, AcctGatewayCash), capture},
				{merchantAccount(p.MerchantID, AcctPayable), -(capture - fee)},
				{merchantAccount(p.MerchantID, AcctFeeRevenue), -fee},
			}
			if err := postEntries(ctx, q, now, "capture", "payment", p.ID, legs); err != nil {
				return err
			}
			return audit(ctx, q, actor, &p.MerchantID, "payment.capture", "payment",
				p.ID.String(), []byte(fmt.Sprintf(`{"captured":%d,"fee":%d}`, capture, fee)))
		},
		func() actionResult {
			return actionResult{resourceID: in.PaymentID, payload: toPaymentView(out)}
		})
	if err != nil {
		return db.Transaction{}, false, err
	}
	_ = body
	// Replay or not, return the current row state.
	p, gerr := s.q.GetPayment(ctx, in.PaymentID)
	return p, replayed, gerr
}

// Void cancels an authorization within the 24h window. No money moved, so no
// ledger entries are posted.
func (s *Service) Void(ctx context.Context, actor Actor, paymentID uuid.UUID, idem Idem) (db.Transaction, bool, error) {
	if actor.Role != "operator" && actor.Role != "admin" {
		return db.Transaction{}, false, domain.ErrForbidden
	}
	var out db.Transaction
	_, replayed, err := s.runIdempotentTx(ctx, actor, "/v1/payments/void", idem,
		func(q *db.Queries) (uuid.UUID, error) {
			p, err := q.GetPayment(ctx, paymentID)
			if errors.Is(err, pgxNoRows) {
				return uuid.Nil, domain.ErrNotFound
			}
			if err != nil {
				return uuid.Nil, err
			}
			if err := actor.requireMerchant(p.MerchantID); err != nil {
				return uuid.Nil, err
			}
			return p.MerchantID, nil
		},
		func(q *db.Queries) error {
			p, err := q.GetPaymentForUpdate(ctx, paymentID)
			if err != nil {
				return err
			}
			if p.Status != domain.StatusAuthorized {
				return fmt.Errorf("%w: payment %s is %s, void requires authorized",
					domain.ErrInvalidState, p.ID, p.Status)
			}
			if !s.clock.Now().Before(p.AuthExpiresAt.Time) {
				return domain.ErrAuthExpired
			}
			out, err = q.MarkVoided(ctx, p.ID)
			if err != nil {
				return err
			}
			return audit(ctx, q, actor, &p.MerchantID, "payment.void", "payment", p.ID.String(), []byte("{}"))
		},
		func() actionResult {
			return actionResult{resourceID: paymentID, payload: toPaymentView(out)}
		})
	if err != nil {
		return db.Transaction{}, false, err
	}
	p, gerr := s.q.GetPayment(ctx, paymentID)
	return p, replayed, gerr
}

// RefundInput issues a full or partial refund.
type RefundInput struct {
	PaymentID   uuid.UUID
	AmountCents int64  `json:"amount_cents"` // 0 => refund all remaining captured
	Reason      string `json:"reason,omitempty"`
}

type refundView struct {
	ID             string `json:"id"`
	PaymentID      string `json:"payment_id"`
	AmountCents    int64  `json:"amount_cents"`
	FeeRefundCents int64  `json:"fee_refund_cents"`
	NetCents       int64  `json:"net_cents"`
	Reason         string `json:"reason"`
	CreatedAt      string `json:"created_at"`
}

func toRefundView(r db.Refund) refundView {
	return refundView{
		ID:             r.ID.String(),
		PaymentID:      r.PaymentID.String(),
		AmountCents:    r.AmountCents,
		FeeRefundCents: r.FeeRefundCents,
		NetCents:       r.NetCents,
		Reason:         r.Reason,
		CreatedAt:      r.CreatedAt.Time.Format("2006-01-02T15:04:05Z07:00"),
	}
}

// Refund issues a (partial) refund, idempotent on idem. Concurrent refunds
// serialize on the payment row; cumulative refunds can never exceed captured.
func (s *Service) Refund(ctx context.Context, actor Actor, in RefundInput, idem Idem) (db.Refund, bool, error) {
	if actor.Role != "operator" && actor.Role != "admin" {
		return db.Refund{}, false, domain.ErrForbidden
	}
	if in.AmountCents < 0 {
		return db.Refund{}, false, domain.ErrAmountInvalid
	}
	var out db.Refund
	_, replayed, err := s.runIdempotentTx(ctx, actor, "/v1/payments/refund", idem,
		func(q *db.Queries) (uuid.UUID, error) {
			p, err := q.GetPayment(ctx, in.PaymentID)
			if errors.Is(err, pgxNoRows) {
				return uuid.Nil, domain.ErrNotFound
			}
			if err != nil {
				return uuid.Nil, err
			}
			if err := actor.requireMerchant(p.MerchantID); err != nil {
				return uuid.Nil, err
			}
			return p.MerchantID, nil
		},
		func(q *db.Queries) error {
			p, err := q.GetPaymentForUpdate(ctx, in.PaymentID)
			if err != nil {
				return err
			}

			switch p.Status {
			case domain.StatusCaptured, domain.StatusPartiallyRefunded, domain.StatusSettled:
			default:
				return fmt.Errorf("%w: payment %s is %s, cannot refund",
					domain.ErrInvalidState, p.ID, p.Status)
			}
			// Refunds after settlement are only accepted within 90 days.
			// Status alone is insufficient (a settled payment becomes
			// partially_refunded after its first post-settlement refund);
			// settled_at is the reliable marker.
			if p.SettledAt.Valid && p.RefundDeadline.Valid &&
				!s.clock.Now().Before(p.RefundDeadline.Time) {
				return domain.ErrRefundWindowClosed
			}

			amount := in.AmountCents
			if amount == 0 {
				amount = p.CapturedCents - p.RefundedCents
			}
			if amount <= 0 {
				return domain.ErrAmountInvalid
			}
			if p.RefundedCents+amount > p.CapturedCents {
				return domain.ErrRefundTooLarge
			}

			m, err := q.GetMerchant(ctx, p.MerchantID)
			if err != nil {
				return err
			}
			bps, _ := merchantFee(m)
			feeRefund := money.RefundFeeDelta(p.CapturedCents, p.RefundedCents, amount, bps)
			net := amount - feeRefund

			r, err := q.CreateRefund(ctx, db.CreateRefundParams{
				PaymentID:      p.ID,
				MerchantID:     p.MerchantID,
				AmountCents:    amount,
				FeeRefundCents: feeRefund,
				NetCents:       net,
				Reason:         in.Reason,
				CreatedAt:      pgtype.Timestamptz{Time: s.clock.Now(), Valid: true},
			})
			if err != nil {
				return err
			}
			if _, err := q.ApplyRefund(ctx, db.ApplyRefundParams{
				ID:               p.ID,
				RefundedCents:    amount,
				RefundedFeeCents: feeRefund,
			}); err != nil {
				return err
			}

			// Ledger, balanced:
			//   gateway_cash -amount       (cash returned to cardholder)
			//   fee_revenue  +feeRefund    (withheld fee handed back)
			//   payable      +net          (merchant balance clawed back)
			legs := []Posting{
				{merchantAccount(p.MerchantID, AcctGatewayCash), -amount},
				{merchantAccount(p.MerchantID, AcctFeeRevenue), feeRefund},
				{merchantAccount(p.MerchantID, AcctPayable), net},
			}
			if err := postEntries(ctx, q, s.clock.Now(), "refund", "refund", r.ID, legs); err != nil {
				return err
			}
			out = r
			return audit(ctx, q, actor, &p.MerchantID, "payment.refund", "refund",
				r.ID.String(), []byte(fmt.Sprintf(`{"payment":%q,"amount":%d,"fee_refund":%d}`,
					p.ID, amount, feeRefund)))
		},
		func() actionResult {
			return actionResult{resourceID: out.ID, payload: toRefundView(out)}
		})
	if err != nil {
		return db.Refund{}, false, err
	}
	if !replayed {
		return out, false, nil
	}
	// Replay: the stored resource id is the original refund; return it.
	r, gerr := s.lookupStoredRefund(ctx, idem)
	return r, true, gerr
}

// lookupStoredRefund fetches the refund recorded for an idempotency key.
func (s *Service) lookupStoredRefund(ctx context.Context, idem Idem) (db.Refund, error) {
	row, err := s.q.GetIdempotencyByKeyEndpoint(ctx,
		db.GetIdempotencyByKeyEndpointParams{Key: idem.Key, Endpoint: "/v1/payments/refund"})
	if err != nil {
		return db.Refund{}, err
	}
	if row.ResourceID == nil {
		return db.Refund{}, domain.ErrNotFound
	}
	return s.q.GetRefund(ctx, *row.ResourceID)
}

func extractResourceID(raw json.RawMessage) (uuid.UUID, error) {
	var v struct {
		ID string `json:"id"`
	}
	if err := json.Unmarshal(raw, &v); err != nil {
		return uuid.Nil, err
	}
	return uuid.Parse(v.ID)
}

// GetPayment returns a payment if the actor may view it.
func (s *Service) GetPayment(ctx context.Context, actor Actor, id uuid.UUID) (db.Transaction, error) {
	p, err := s.q.GetPayment(ctx, id)
	if errors.Is(err, pgxNoRows) {
		return db.Transaction{}, domain.ErrNotFound
	}
	if err != nil {
		return db.Transaction{}, err
	}
	if err := actor.requireMerchant(p.MerchantID); err != nil {
		return db.Transaction{}, err
	}
	return p, nil
}

// ListPayments returns a merchant's payments.
func (s *Service) ListPayments(ctx context.Context, actor Actor, merchantID uuid.UUID, limit, offset int32) ([]db.Transaction, error) {
	if err := actor.requireMerchant(merchantID); err != nil {
		return nil, err
	}
	if limit <= 0 || limit > 200 {
		limit = 50
	}
	return s.q.ListPaymentsByMerchant(ctx, db.ListPaymentsByMerchantParams{
		MerchantID: merchantID, Limit: limit, Offset: offset,
	})
}

// ListRefunds returns a merchant's refunds.
func (s *Service) ListRefunds(ctx context.Context, actor Actor, merchantID uuid.UUID, limit, offset int32) ([]db.Refund, error) {
	if err := actor.requireMerchant(merchantID); err != nil {
		return nil, err
	}
	if limit <= 0 || limit > 200 {
		limit = 50
	}
	return s.q.ListRefundsByMerchant(ctx, db.ListRefundsByMerchantParams{
		MerchantID: merchantID, Limit: limit, Offset: offset,
	})
}
