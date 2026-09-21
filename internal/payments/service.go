// Package payments implements the payment state machine, idempotency handling
// and double-entry postings for authorize / capture / void / refund.
//
// Allowed transitions:
//
//	authorized -> captured | voided
//	captured   -> partially_refunded | refunded        (refund ops)
//	partially_refunded -> partially_refunded | refunded
//	captured|partially_refunded -> settled             (settlement worker)
//	settled stays "settled" across further refunds, but refunded_* accumulate
//
// Timing rules:
//
//	void only while authorized and within 24h of authorization
//	refund only within 90 days of capture/settlement (see RefundWindow)
//	cumulative refunded_amount <= captured_amount (paid amount)
package payments

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgtype"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/clearsettle/clearsettle/internal/audit"
	"github.com/clearsettle/clearsettle/internal/ledger"
	"github.com/clearsettle/clearsettle/internal/money"
	"github.com/clearsettle/clearsettle/internal/store"
)

const (
	StatusAuthorized        = "authorized"
	StatusCaptured          = "captured"
	StatusPartiallyRefunded = "partially_refunded"
	StatusRefunded          = "refunded"
	StatusVoided            = "voided"
	StatusSettled           = "settled"
	AuthorizeWindow         = 24 * time.Hour
	RefundWindow            = 90 * 24 * time.Hour
	routeAuthorize          = "POST /v1/payments/authorize"
	routeCapture            = "POST /v1/payments/{id}/capture"
	routeVoid               = "POST /v1/payments/{id}/void"
	routeRefund             = "POST /v1/payments/{id}/refund"
)

var (
	ErrNotFound          = errors.New("payment not found")
	ErrConflict          = errors.New("idempotency key conflict")
	ErrIllegalTransition = errors.New("illegal status transition")
	ErrAmount            = errors.New("invalid amount")
	ErrExpiredAuth       = errors.New("authorization expired or outside 24h void window")
	ErrRefundWindow      = errors.New("refund outside 90-day window")
	ErrRefundTooLarge    = errors.New("cumulative refund would exceed captured amount")
)

type Service struct {
	pool *pgxpool.Pool
	q    *store.Queries
}

func New(pool *pgxpool.Pool) *Service {
	return &Service{pool: pool, q: store.New(pool)}
}

// Actor identifies the caller for audit purposes.
type Actor struct {
	UserID *uuid.UUID
	Role   string
	IP     string
}

type AuthorizeInput struct {
	MerchantID     uuid.UUID
	IdempotencyKey string
	Amount         int64
	ExternalRef    string
}

type CaptureInput struct {
	MerchantID     uuid.UUID
	PaymentID      uuid.UUID
	IdempotencyKey string
	Amount         int64 // 0 => full remaining authorized amount
}

type VoidInput struct {
	MerchantID     uuid.UUID
	PaymentID      uuid.UUID
	IdempotencyKey string
}

type RefundInput struct {
	MerchantID     uuid.UUID
	PaymentID      uuid.UUID
	IdempotencyKey string
	Amount         int64
	Reason         string
}

// Replay is a stored idempotent response.
type Replay struct {
	StatusCode int
	Body       json.RawMessage
}

func fingerprint(v any) string {
	b, _ := json.Marshal(v)
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

func isUnique(err error) bool {
	var pgErr *pgconn.PgError
	return errors.As(err, &pgErr) && pgErr.Code == "23505"
}

// lookupIdem returns the stored response (ok=true) or ErrConflict when the key
// exists but carries a different request fingerprint.
func lookupIdem(ctx context.Context, q store.Querier, merchantID uuid.UUID, key, wantHash string) (Replay, bool, error) {
	row, err := q.GetIdempotentRequest(ctx, store.GetIdempotentRequestParams{
		MerchantID: merchantID,
		IdemKey:    key,
	})
	if errors.Is(err, pgx.ErrNoRows) {
		return Replay{}, false, nil
	}
	if err != nil {
		return Replay{}, false, err
	}
	if row.RequestHash != wantHash {
		return Replay{}, false, ErrConflict
	}
	return Replay{StatusCode: int(row.StatusCode), Body: row.ResponseBody}, true, nil
}

func saveIdem(ctx context.Context, q store.Querier, merchantID uuid.UUID, key, route, hash string, status int, body any) error {
	b, err := json.Marshal(body)
	if err != nil {
		return err
	}
	_, err = q.CreateIdempotentRequest(ctx, store.CreateIdempotentRequestParams{
		MerchantID:   merchantID,
		IdemKey:      key,
		Route:        route,
		RequestHash:  hash,
		StatusCode:   int16orInt(status),
		ResponseBody: b,
	})
	return err
}

func int16orInt(n int) int32 { return int32(n) }

// ---------------------------------------------------------------- authorize

func (s *Service) Authorize(ctx context.Context, in AuthorizeInput, actor Actor) (store.Payment, Replay, error) {
	var replay Replay
	if in.Amount <= 0 {
		return store.Payment{}, replay, ErrAmount
	}
	hash := fingerprint(map[string]any{"amount": in.Amount, "external_ref": in.ExternalRef, "op": "authorize"})
	var out store.Payment
	err := s.tx(ctx, func(q store.Querier, tx pgx.Tx) error {
		if r, ok, err := lookupIdem(ctx, q, in.MerchantID, in.IdempotencyKey, hash); err != nil {
			return err
		} else if ok {
			replay = r
			return errReplay
		}
		p, err := q.CreatePayment(ctx, store.CreatePaymentParams{
			MerchantID:       in.MerchantID,
			IdempotencyKey:   in.IdempotencyKey,
			AuthorizedAmount: in.Amount,
			FeeBps:           0,
			FeeFixed:         0,
			ExternalRef:      nullable(in.ExternalRef),
			Secs:             float64(AuthorizeWindow / time.Second),
		})
		if err != nil {
			return mapDupe(err)
		}
		if err := audit.Write(ctx, q, audit.Entry{
			ActorUser: actor.UserID, ActorRole: actor.Role, MerchantID: &in.MerchantID,
			Action: "payment.authorize", TargetType: "payment", TargetID: p.ID.String(),
			Detail: map[string]any{"amount": in.Amount}, IP: actor.IP,
		}); err != nil {
			return err
		}
		resp := paymentResponse(p)
		if err := saveIdem(ctx, q, in.MerchantID, in.IdempotencyKey, routeAuthorize, hash, 201, resp); err != nil {
			return mapDupe(err)
		}
		out = p
		return nil
	})
	if errors.Is(err, errReplay) {
		return out, replay, nil
	}
	if errors.Is(err, ErrConflict) {
		r, rerr := s.resolveRace(ctx, in.MerchantID, in.IdempotencyKey, hash)
		if rerr == nil {
			return out, r, nil
		}
		return out, Replay{}, rerr
	}
	return out, replay, err
}

// ----------------------------------------------------------------- capture

func (s *Service) Capture(ctx context.Context, in CaptureInput, actor Actor) (store.Payment, Replay, error) {
	hash := fingerprint(map[string]any{"payment_id": in.PaymentID.String(), "amount": in.Amount, "op": "capture"})
	var out store.Payment
	var replay Replay
	err := s.tx(ctx, func(q store.Querier, tx pgx.Tx) error {
		if r, ok, err := lookupIdem(ctx, q, in.MerchantID, in.IdempotencyKey, hash); err != nil {
			return err
		} else if ok {
			replay = r
			return errReplay
		}
		p, err := q.GetPaymentForUpdate(ctx, store.GetPaymentForUpdateParams{
			ID: in.PaymentID, MerchantID: in.MerchantID,
		})
		if errors.Is(err, pgx.ErrNoRows) {
			return ErrNotFound
		}
		if err != nil {
			return err
		}
		if p.Status != StatusAuthorized {
			return fmt.Errorf("%w: %s -> capture", ErrIllegalTransition, p.Status)
		}
		if time.Now().After(p.ExpiresAt.Time) {
			return ErrExpiredAuth
		}
		amount := in.Amount
		if amount == 0 {
			amount = p.AuthorizedAmount
		}
		if amount <= 0 || amount > p.AuthorizedAmount {
			return ErrAmount
		}
		// fee schedule is fixed on the merchant at capture time
		m, err := q.GetMerchant(ctx, in.MerchantID)
		if err != nil {
			return err
		}
		fee := int64(money.Fee(money.Cents(amount), int(m.FeeBps), money.Cents(m.FeeFixed)))
		captured, err := q.CapturePayment(ctx, store.CapturePaymentParams{
			ID: in.PaymentID, MerchantID: in.MerchantID,
			CapturedAmount: amount, FeeAmount: fee,
		})
		if err != nil {
			return mapDupe(err)
		}
		// Posting: Cash +amount, Fee revenue +fee, Payable +(amount-fee).
		accts, err := ledger.LoadAccounts(ctx, q, in.MerchantID)
		if err != nil {
			return err
		}
		if err := ledger.Post(ctx, tx, q, ledger.Posting{
			RefType: ledger.RefTypePayment, RefID: captured.ID,
			Lines: []ledger.Line{
				{AccountID: accts.Cash.ID, Amount: amount},
				{AccountID: accts.Fees.ID, Amount: -fee},
				{AccountID: accts.Payable.ID, Amount: -(amount - fee)},
			},
		}); err != nil {
			return err
		}
		if err := q.InsertChannelEvent(ctx, store.InsertChannelEventParams{
			MerchantID:  in.MerchantID,
			EventDate:   dateArg(time.Now().UTC()),
			EventType:   "capture",
			PaymentID:   captured.ID,
			GrossAmount: amount,
			FeeDelta:    fee,
		}); err != nil {
			return err
		}
		if err := audit.Write(ctx, q, audit.Entry{
			ActorUser: actor.UserID, ActorRole: actor.Role, MerchantID: &in.MerchantID,
			Action: "payment.capture", TargetType: "payment", TargetID: captured.ID.String(),
			Detail: map[string]any{"amount": amount, "fee": fee}, IP: actor.IP,
		}); err != nil {
			return err
		}
		resp := paymentResponse(captured)
		if err := saveIdem(ctx, q, in.MerchantID, in.IdempotencyKey, routeCapture, hash, 200, resp); err != nil {
			return mapDupe(err)
		}
		out = captured
		return nil
	})
	if errors.Is(err, errReplay) {
		return out, replay, nil
	}
	if errors.Is(err, ErrConflict) {
		r, rerr := s.resolveRace(ctx, in.MerchantID, in.IdempotencyKey, hash)
		if rerr == nil {
			return out, r, nil
		}
		return out, Replay{}, rerr
	}
	return out, replay, err
}

// -------------------------------------------------------------------- void

func (s *Service) Void(ctx context.Context, in VoidInput, actor Actor) (store.Payment, Replay, error) {
	hash := fingerprint(map[string]any{"payment_id": in.PaymentID.String(), "op": "void"})
	var out store.Payment
	var replay Replay
	err := s.tx(ctx, func(q store.Querier, tx pgx.Tx) error {
		if r, ok, err := lookupIdem(ctx, q, in.MerchantID, in.IdempotencyKey, hash); err != nil {
			return err
		} else if ok {
			replay = r
			return errReplay
		}
		p, err := q.GetPaymentForUpdate(ctx, store.GetPaymentForUpdateParams{
			ID: in.PaymentID, MerchantID: in.MerchantID,
		})
		if errors.Is(err, pgx.ErrNoRows) {
			return ErrNotFound
		}
		if err != nil {
			return err
		}
		if p.Status != StatusAuthorized {
			return fmt.Errorf("%w: %s -> void (only authorized payments can be voided)", ErrIllegalTransition, p.Status)
		}
		// expires_at is the single source of truth for the 24h window
		// (authorized_at + AuthorizeWindow).
		if time.Now().After(p.ExpiresAt.Time) {
			return ErrExpiredAuth
		}
		v, err := q.VoidPayment(ctx, store.VoidPaymentParams{ID: in.PaymentID, MerchantID: in.MerchantID})
		if err != nil {
			return err
		}
		if err := audit.Write(ctx, q, audit.Entry{
			ActorUser: actor.UserID, ActorRole: actor.Role, MerchantID: &in.MerchantID,
			Action: "payment.void", TargetType: "payment", TargetID: v.ID.String(),
			Detail: map[string]any{"authorized_amount": p.AuthorizedAmount}, IP: actor.IP,
		}); err != nil {
			return err
		}
		resp := paymentResponse(v)
		if err := saveIdem(ctx, q, in.MerchantID, in.IdempotencyKey, routeVoid, hash, 200, resp); err != nil {
			return mapDupe(err)
		}
		out = v
		return nil
	})
	if errors.Is(err, errReplay) {
		return out, replay, nil
	}
	if errors.Is(err, ErrConflict) {
		r, rerr := s.resolveRace(ctx, in.MerchantID, in.IdempotencyKey, hash)
		if rerr == nil {
			return out, r, nil
		}
		return out, Replay{}, rerr
	}
	return out, replay, err
}

// ------------------------------------------------------------------ refund

func (s *Service) Refund(ctx context.Context, in RefundInput, actor Actor) (store.Refund, store.Payment, Replay, error) {
	hash := fingerprint(map[string]any{"payment_id": in.PaymentID.String(), "amount": in.Amount, "reason": in.Reason, "op": "refund"})
	var outR store.Refund
	var outP store.Payment
	var replay Replay
	err := s.tx(ctx, func(q store.Querier, tx pgx.Tx) error {
		if r, ok, err := lookupIdem(ctx, q, in.MerchantID, in.IdempotencyKey, hash); err != nil {
			return err
		} else if ok {
			replay = r
			return errReplay
		}
		if in.Amount <= 0 {
			return ErrAmount
		}
		p, err := q.GetPaymentForUpdate(ctx, store.GetPaymentForUpdateParams{
			ID: in.PaymentID, MerchantID: in.MerchantID,
		})
		if errors.Is(err, pgx.ErrNoRows) {
			return ErrNotFound
		}
		if err != nil {
			return err
		}
		switch p.Status {
		case StatusCaptured, StatusPartiallyRefunded, StatusSettled:
		default:
			return fmt.Errorf("%w: %s -> refund", ErrIllegalTransition, p.Status)
		}
		// 90-day window: measured from capture; for already-settled payments
		// also bounded by settlement completion (same instant for the first batch).
		if time.Since(p.CapturedAt.Time) > RefundWindow {
			return ErrRefundWindow
		}
		remaining := p.CapturedAmount - p.RefundedAmount
		if in.Amount > remaining {
			return ErrRefundTooLarge
		}
		last := in.Amount == remaining
		feeBack := money.RefundFee(money.Cents(p.FeeAmount), money.Cents(in.Amount),
			money.Cents(p.CapturedAmount), money.Cents(p.RefundedFee), last)
		r, err := q.CreateRefund(ctx, store.CreateRefundParams{
			PaymentID:      in.PaymentID,
			MerchantID:     in.MerchantID,
			IdempotencyKey: in.IdempotencyKey,
			Amount:         in.Amount,
			FeeRefund:      int64(feeBack),
			Reason:         nullable(in.Reason),
		})
		if err != nil {
			return mapDupe(err)
		}
		upd, err := q.ApplyRefund(ctx, store.ApplyRefundParams{
			ID: in.PaymentID, MerchantID: in.MerchantID,
			RefundedAmount: in.Amount, RefundedFee: int64(feeBack),
		})
		if err != nil {
			return err
		}
		accts, err := ledger.LoadAccounts(ctx, q, in.MerchantID)
		if err != nil {
			return err
		}
		// Refund posting (after settlement payable can go negative — it nets in
		// the next payout). Before settlement it simply reduces the liability.
		// Cash -amount; Fees +feeBack (revenue reversal); Payable +(amount-feeBack).
		if err := ledger.Post(ctx, tx, q, ledger.Posting{
			RefType: ledger.RefTypeRefund, RefID: r.ID,
			Lines: []ledger.Line{
				{AccountID: accts.Cash.ID, Amount: -in.Amount},
				{AccountID: accts.Fees.ID, Amount: int64(feeBack)},
				{AccountID: accts.Payable.ID, Amount: in.Amount - int64(feeBack)},
			},
		}); err != nil {
			return err
		}
		if err := q.InsertChannelEvent(ctx, store.InsertChannelEventParams{
			MerchantID:  in.MerchantID,
			EventDate:   dateArg(time.Now().UTC()),
			EventType:   "refund",
			PaymentID:   in.PaymentID,
			RefundID:    &r.ID,
			GrossAmount: in.Amount,
			FeeDelta:    -int64(feeBack),
		}); err != nil {
			return err
		}
		if err := audit.Write(ctx, q, audit.Entry{
			ActorUser: actor.UserID, ActorRole: actor.Role, MerchantID: &in.MerchantID,
			Action: "payment.refund", TargetType: "refund", TargetID: r.ID.String(),
			Detail: map[string]any{"payment_id": in.PaymentID.String(), "amount": in.Amount, "fee_refund": int64(feeBack)},
			IP:     actor.IP,
		}); err != nil {
			return err
		}
		body := map[string]any{"refund": refundResponse(r), "payment": paymentResponse(upd)}
		if err := saveIdem(ctx, q, in.MerchantID, in.IdempotencyKey, routeRefund, hash, 201, body); err != nil {
			return mapDupe(err)
		}
		outR, outP = r, upd
		return nil
	})
	if errors.Is(err, errReplay) {
		return outR, outP, replay, nil
	}
	if errors.Is(err, ErrConflict) {
		r, rerr := s.resolveRace(ctx, in.MerchantID, in.IdempotencyKey, hash)
		if rerr == nil {
			return outR, outP, r, nil
		}
		return outR, outP, Replay{}, rerr
	}
	return outR, outP, replay, err
}

// ----------------------------------------------------------------- helpers

var errReplay = errors.New("idempotent replay")

// resolveRace is called after a transaction lost the unique-key race. The
// winning transaction either committed the idempotent row (same hash => replay,
// different hash => 409) or is still in flight; in the latter case we briefly
// wait and re-check, then surface a retryable conflict.
func (s *Service) resolveRace(ctx context.Context, merchantID uuid.UUID, key, hash string) (Replay, error) {
	for i := 0; i < 10; i++ {
		r, ok, err := lookupIdem(ctx, s.q, merchantID, key, hash)
		if err != nil && !errors.Is(err, ErrConflict) {
			return Replay{}, err
		}
		if ok {
			return r, nil
		}
		if err != nil {
			return Replay{}, err // differing fingerprint
		}
		select {
		case <-ctx.Done():
			return Replay{}, ctx.Err()
		case <-time.After(25 * time.Millisecond):
		}
	}
	return Replay{}, ErrConflict
}

func (s *Service) tx(ctx context.Context, fn func(store.Querier, pgx.Tx) error) error {
	return pgx.BeginTxFunc(ctx, s.pool, pgx.TxOptions{}, func(tx pgx.Tx) error {
		return fn(store.New(tx), tx)
	})
}

func mapDupe(err error) error {
	if isUnique(err) {
		// Concurrent insert of same (merchant, idem key) — replay path wins;
		// caller should re-read. Treat as conflict for differing fingerprints
		// only; a pure race becomes a retryable error surfaced as 409 with a
		// retry hint.
		return ErrConflict
	}
	return err
}

func nullable(s string) pgtype.Text {
	if s == "" {
		return pgtype.Text{Valid: false}
	}
	return pgtype.Text{String: s, Valid: true}
}
