package payments

import (
	"time"

	"github.com/jackc/pgx/v5/pgtype"

	"github.com/clearsettle/clearsettle/internal/store"
)

func dateArg(t time.Time) pgtype.Date {
	t = t.UTC()
	return pgtype.Date{Time: time.Date(t.Year(), t.Month(), t.Day(), 0, 0, 0, 0, time.UTC), Valid: true}
}

// PaymentResponse is the wire representation; timestamps are RFC3339 UTC.
type PaymentResponse struct {
	ID               string `json:"id"`
	Status           string `json:"status"`
	AuthorizedAmount int64  `json:"authorized_amount"`
	CapturedAmount   int64  `json:"captured_amount"`
	FeeAmount        int64  `json:"fee_amount"`
	RefundedAmount   int64  `json:"refunded_amount"`
	RefundedFee      int64  `json:"refunded_fee"`
	Currency         string `json:"currency"`
	ExternalRef      string `json:"external_ref,omitempty"`
	AuthorizedAt     string `json:"authorized_at"`
	ExpiresAt        string `json:"expires_at"`
	CapturedAt       string `json:"captured_at,omitempty"`
	Settled          bool   `json:"settled"`
}

func ts(v pgtype.Timestamptz) string {
	if !v.Valid {
		return ""
	}
	return v.Time.UTC().Format(time.RFC3339)
}

func paymentResponse(p store.Payment) PaymentResponse {
	return PaymentResponse{
		ID:               p.ID.String(),
		Status:           p.Status,
		AuthorizedAmount: p.AuthorizedAmount,
		CapturedAmount:   p.CapturedAmount,
		FeeAmount:        p.FeeAmount,
		RefundedAmount:   p.RefundedAmount,
		RefundedFee:      p.RefundedFee,
		Currency:         p.Currency,
		ExternalRef:      p.ExternalRef.String,
		AuthorizedAt:     ts(p.AuthorizedAt),
		ExpiresAt:        ts(p.ExpiresAt),
		CapturedAt:       ts(p.CapturedAt),
		Settled:          p.SettlementBatchID != nil,
	}
}

// EncodePayment is the exported wire mapper for use by read handlers.
func EncodePayment(p store.Payment) PaymentResponse { return paymentResponse(p) }

type RefundResponse struct {
	ID        string `json:"id"`
	PaymentID string `json:"payment_id"`
	Amount    int64  `json:"amount"`
	FeeRefund int64  `json:"fee_refund"`
	Reason    string `json:"reason,omitempty"`
	CreatedAt string `json:"created_at"`
}

func refundResponse(r store.Refund) RefundResponse {
	return RefundResponse{
		ID:        r.ID.String(),
		PaymentID: r.PaymentID.String(),
		Amount:    r.Amount,
		FeeRefund: r.FeeRefund,
		Reason:    r.Reason.String,
		CreatedAt: ts(r.CreatedAt),
	}
}

// EncodeRefund is the exported wire mapper for read/reply handlers.
func EncodeRefund(r store.Refund) RefundResponse { return refundResponse(r) }
