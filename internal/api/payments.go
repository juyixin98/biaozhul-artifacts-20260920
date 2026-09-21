package api

import (
	"encoding/json"
	"net/http"

	"github.com/go-chi/chi/v5"
	"github.com/google/uuid"

	"github.com/clearsettle/clearsettle/internal/domain"
	"github.com/clearsettle/clearsettle/internal/service"
)

type authorizeRequest struct {
	MerchantID  string `json:"merchant_id"`
	AmountCents int64  `json:"amount_cents"`
	CardLast4   string `json:"card_last4"`
}

func (s *Server) authorize(w http.ResponseWriter, r *http.Request) {
	idem, body, err := idemFromRequest(r)
	if err != nil {
		writeErr(w, domain.ErrValidation)
		return
	}
	var req authorizeRequest
	if err := json.Unmarshal(body, &req); err != nil {
		writeErr(w, domain.ErrValidation)
		return
	}
	mid, err := uuid.Parse(req.MerchantID)
	if err != nil {
		writeErr(w, domain.ErrValidation)
		return
	}
	p, replayed, err := s.svc.Authorize(r.Context(), actorFrom(r), service.AuthorizeInput{
		MerchantID:  mid,
		AmountCents: req.AmountCents,
		CardLast4:   req.CardLast4,
	}, idem)
	if err != nil {
		writeErr(w, err)
		return
	}
	status := http.StatusCreated
	if replayed {
		status = http.StatusOK
	}
	writeJSON(w, status, paymentMasked(p))
}

type captureRequest struct {
	PaymentID    string `json:"payment_id"`
	CaptureCents int64  `json:"capture_cents,omitempty"`
}

func (s *Server) capture(w http.ResponseWriter, r *http.Request) {
	idem, body, err := idemFromRequest(r)
	if err != nil {
		writeErr(w, domain.ErrValidation)
		return
	}
	var req captureRequest
	if err := json.Unmarshal(body, &req); err != nil {
		writeErr(w, domain.ErrValidation)
		return
	}
	pid, err := uuid.Parse(req.PaymentID)
	if err != nil {
		writeErr(w, domain.ErrValidation)
		return
	}
	p, replayed, err := s.svc.Capture(r.Context(), actorFrom(r), service.CaptureInput{
		PaymentID:    pid,
		CaptureCents: req.CaptureCents,
	}, idem)
	if err != nil {
		writeErr(w, err)
		return
	}
	status := http.StatusOK
	if !replayed {
		status = http.StatusCreated
	}
	writeJSON(w, status, paymentMasked(p))
}

type voidRequest struct {
	PaymentID string `json:"payment_id"`
}

func (s *Server) void(w http.ResponseWriter, r *http.Request) {
	idem, body, err := idemFromRequest(r)
	if err != nil {
		writeErr(w, domain.ErrValidation)
		return
	}
	var req voidRequest
	if err := json.Unmarshal(body, &req); err != nil {
		writeErr(w, domain.ErrValidation)
		return
	}
	pid, err := uuid.Parse(req.PaymentID)
	if err != nil {
		writeErr(w, domain.ErrValidation)
		return
	}
	p, _, err := s.svc.Void(r.Context(), actorFrom(r), pid, idem)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, paymentMasked(p))
}

type refundRequest struct {
	PaymentID   string `json:"payment_id"`
	AmountCents int64  `json:"amount_cents,omitempty"`
	Reason      string `json:"reason,omitempty"`
}

func (s *Server) refund(w http.ResponseWriter, r *http.Request) {
	idem, body, err := idemFromRequest(r)
	if err != nil {
		writeErr(w, domain.ErrValidation)
		return
	}
	var req refundRequest
	if err := json.Unmarshal(body, &req); err != nil {
		writeErr(w, domain.ErrValidation)
		return
	}
	pid, err := uuid.Parse(req.PaymentID)
	if err != nil {
		writeErr(w, domain.ErrValidation)
		return
	}
	ref, _, err := s.svc.Refund(r.Context(), actorFrom(r), service.RefundInput{
		PaymentID:   pid,
		AmountCents: req.AmountCents,
		Reason:      req.Reason,
	}, idem)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{
		"id":               ref.ID.String(),
		"payment_id":       ref.PaymentID.String(),
		"amount_cents":     ref.AmountCents,
		"fee_refund_cents": ref.FeeRefundCents,
		"net_cents":        ref.NetCents,
		"reason":           ref.Reason,
		"created_at":       ref.CreatedAt.Time.Format("2006-01-02T15:04:05Z07:00"),
	})
}

func (s *Server) getPayment(w http.ResponseWriter, r *http.Request) {
	id, err := uuid.Parse(chi.URLParam(r, "paymentID"))
	if err != nil {
		writeErr(w, domain.ErrValidation)
		return
	}
	p, err := s.svc.GetPayment(r.Context(), actorFrom(r), id)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, paymentMasked(p))
}

func (s *Server) listPayments(w http.ResponseWriter, r *http.Request) {
	mid, err := uuid.Parse(chi.URLParam(r, "merchantID"))
	if err != nil {
		writeErr(w, domain.ErrValidation)
		return
	}
	rows, err := s.svc.ListPayments(r.Context(), actorFrom(r), mid,
		int32(atoiDefault(r.URL.Query().Get("limit"), 50)),
		int32(atoiDefault(r.URL.Query().Get("offset"), 0)))
	if err != nil {
		writeErr(w, err)
		return
	}
	out := make([]map[string]any, 0, len(rows))
	for _, p := range rows {
		out = append(out, paymentMasked(p))
	}
	writeJSON(w, http.StatusOK, map[string]any{"payments": out})
}

func (s *Server) listRefunds(w http.ResponseWriter, r *http.Request) {
	mid, err := uuid.Parse(chi.URLParam(r, "merchantID"))
	if err != nil {
		writeErr(w, domain.ErrValidation)
		return
	}
	rows, err := s.svc.ListRefunds(r.Context(), actorFrom(r), mid,
		int32(atoiDefault(r.URL.Query().Get("limit"), 50)),
		int32(atoiDefault(r.URL.Query().Get("offset"), 0)))
	if err != nil {
		writeErr(w, err)
		return
	}
	out := make([]map[string]any, 0, len(rows))
	for _, ref := range rows {
		out = append(out, map[string]any{
			"id":               ref.ID.String(),
			"payment_id":       ref.PaymentID.String(),
			"amount_cents":     ref.AmountCents,
			"fee_refund_cents": ref.FeeRefundCents,
			"net_cents":        ref.NetCents,
			"created_at":       ref.CreatedAt.Time.Format("2006-01-02T15:04:05Z07:00"),
		})
	}
	writeJSON(w, http.StatusOK, map[string]any{"refunds": out})
}
