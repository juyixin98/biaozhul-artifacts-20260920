package api

import (
	"encoding/json"
	"errors"
	"net/http"

	"github.com/go-chi/chi/v5"
	"github.com/google/uuid"

	"github.com/clearsettle/clearsettle/internal/audit"
	"github.com/clearsettle/clearsettle/internal/auth"
	"github.com/clearsettle/clearsettle/internal/payments"
)

type loginReq struct {
	Email    string `json:"email"`
	Password string `json:"password"`
}

func (d Deps) handleLogin(w http.ResponseWriter, r *http.Request) {
	var req loginReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "bad_json", err.Error())
		return
	}
	u, err := d.Admin.AuthenticateUser(r.Context(), req.Email, req.Password, clientIP(r))
	if err != nil {
		writeError(w, http.StatusUnauthorized, "invalid_credentials", "email or password incorrect")
		return
	}
	c := auth.Claims{Sub: u.ID.String(), Role: u.Role, Email: u.Email}
	if u.MerchantID != nil {
		c.MerchantID = u.MerchantID.String()
	}
	tok, err := auth.IssueUserToken(d.JWTSecret, c)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "token_failed", err.Error())
		return
	}
	_ = d.Q
	writeJSON(w, http.StatusOK, map[string]any{
		"token": tok, "role": u.Role, "email": u.Email,
		"merchant_id": c.MerchantID,
	})
}

// ---------------------------------------------------------------- payments

type authorizeReq struct {
	IdempotencyKey string `json:"idempotency_key"`
	Amount         int64  `json:"amount"`
	ExternalRef    string `json:"external_ref"`
}

func idemKey(r *http.Request) string {
	if k := r.Header.Get("Idempotency-Key"); k != "" {
		return k
	}
	return ""
}

func (d Deps) actor(r *http.Request) payments.Actor {
	p := principalFrom(r.Context())
	return payments.Actor{UserID: p.UserID, Role: p.Role, IP: clientIP(r)}
}

func writePaymentError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, payments.ErrConflict):
		writeError(w, http.StatusConflict, "idempotency_conflict",
			"an idempotency key was reused with different parameters")
	case errors.Is(err, payments.ErrIllegalTransition):
		writeError(w, http.StatusConflict, "illegal_transition", err.Error())
	case errors.Is(err, payments.ErrNotFound):
		writeError(w, http.StatusNotFound, "not_found", err.Error())
	case errors.Is(err, payments.ErrAmount):
		writeError(w, http.StatusBadRequest, "invalid_amount", "amount must be positive and within the authorized limit")
	case errors.Is(err, payments.ErrExpiredAuth):
		writeError(w, http.StatusConflict, "authorization_expired", "24h authorization window elapsed")
	case errors.Is(err, payments.ErrRefundWindow):
		writeError(w, http.StatusConflict, "refund_window_closed", "refunds are allowed only within 90 days")
	case errors.Is(err, payments.ErrRefundTooLarge):
		writeError(w, http.StatusUnprocessableEntity, "refund_too_large", "cumulative refund exceeds captured amount")
	default:
		writeError(w, http.StatusInternalServerError, "internal", err.Error())
	}
}

func (d Deps) handleAuthorize(w http.ResponseWriter, r *http.Request) {
	p := principalFrom(r.Context())
	var req authorizeReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "bad_json", err.Error())
		return
	}
	key := firstNonEmpty(idemKey(r), req.IdempotencyKey)
	if key == "" {
		writeError(w, http.StatusBadRequest, "missing_idempotency_key", "Idempotency-Key header required")
		return
	}
	out, replay, err := d.Payments.Authorize(r.Context(), payments.AuthorizeInput{
		MerchantID:     *p.MerchantID,
		IdempotencyKey: key,
		Amount:         req.Amount,
		ExternalRef:    req.ExternalRef,
	}, d.actor(r))
	if err != nil {
		writePaymentError(w, err)
		return
	}
	if replay.Body != nil {
		w.Header().Set("Idempotent-Replay", "true")
		writeRawJSON(w, replay.StatusCode, replay.Body)
		return
	}
	w.Header().Set("Idempotent-Replay", "false")
	writeJSON(w, http.StatusCreated, payments.EncodePayment(out))
}

type captureReq struct {
	IdempotencyKey string `json:"idempotency_key"`
	Amount         int64  `json:"amount"`
}

func (d Deps) handleCapture(w http.ResponseWriter, r *http.Request) {
	p := principalFrom(r.Context())
	id, err := uuid.Parse(chi.URLParam(r, "id"))
	if err != nil {
		writeError(w, http.StatusBadRequest, "bad_id", "payment id must be a UUID")
		return
	}
	var req captureReq
	_ = json.NewDecoder(r.Body).Decode(&req)
	key := firstNonEmpty(idemKey(r), req.IdempotencyKey)
	if key == "" {
		writeError(w, http.StatusBadRequest, "missing_idempotency_key", "Idempotency-Key header required")
		return
	}
	out, replay, err := d.Payments.Capture(r.Context(), payments.CaptureInput{
		MerchantID: *p.MerchantID, PaymentID: id, IdempotencyKey: key, Amount: req.Amount,
	}, d.actor(r))
	if err != nil {
		writePaymentError(w, err)
		return
	}
	if replay.Body != nil {
		w.Header().Set("Idempotent-Replay", "true")
		writeRawJSON(w, replay.StatusCode, replay.Body)
		return
	}
	w.Header().Set("Idempotent-Replay", "false")
	writeJSON(w, http.StatusOK, payments.EncodePayment(out))
}

type voidReq struct {
	IdempotencyKey string `json:"idempotency_key"`
}

func (d Deps) handleVoid(w http.ResponseWriter, r *http.Request) {
	p := principalFrom(r.Context())
	id, err := uuid.Parse(chi.URLParam(r, "id"))
	if err != nil {
		writeError(w, http.StatusBadRequest, "bad_id", "payment id must be a UUID")
		return
	}
	var req voidReq
	_ = json.NewDecoder(r.Body).Decode(&req)
	key := firstNonEmpty(idemKey(r), req.IdempotencyKey)
	if key == "" {
		writeError(w, http.StatusBadRequest, "missing_idempotency_key", "Idempotency-Key header required")
		return
	}
	out, replay, err := d.Payments.Void(r.Context(), payments.VoidInput{
		MerchantID: *p.MerchantID, PaymentID: id, IdempotencyKey: key,
	}, d.actor(r))
	if err != nil {
		writePaymentError(w, err)
		return
	}
	if replay.Body != nil {
		w.Header().Set("Idempotent-Replay", "true")
		writeRawJSON(w, replay.StatusCode, replay.Body)
		return
	}
	w.Header().Set("Idempotent-Replay", "false")
	writeJSON(w, http.StatusOK, payments.EncodePayment(out))
}

type refundReq struct {
	IdempotencyKey string `json:"idempotency_key"`
	Amount         int64  `json:"amount"`
	Reason         string `json:"reason"`
}

func (d Deps) handleRefund(w http.ResponseWriter, r *http.Request) {
	p := principalFrom(r.Context())
	id, err := uuid.Parse(chi.URLParam(r, "id"))
	if err != nil {
		writeError(w, http.StatusBadRequest, "bad_id", "payment id must be a UUID")
		return
	}
	var req refundReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "bad_json", err.Error())
		return
	}
	key := firstNonEmpty(idemKey(r), req.IdempotencyKey)
	if key == "" {
		writeError(w, http.StatusBadRequest, "missing_idempotency_key", "Idempotency-Key header required")
		return
	}
	ref, pay, replay, err := d.Payments.Refund(r.Context(), payments.RefundInput{
		MerchantID: *p.MerchantID, PaymentID: id, IdempotencyKey: key,
		Amount: req.Amount, Reason: req.Reason,
	}, d.actor(r))
	if err != nil {
		writePaymentError(w, err)
		return
	}
	if replay.Body != nil {
		w.Header().Set("Idempotent-Replay", "true")
		writeRawJSON(w, replay.StatusCode, replay.Body)
		return
	}
	w.Header().Set("Idempotent-Replay", "false")
	writeJSON(w, http.StatusCreated, map[string]any{
		"refund":  payments.EncodeRefund(ref),
		"payment": payments.EncodePayment(pay),
	})
}

func firstNonEmpty(vals ...string) string {
	for _, v := range vals {
		if v != "" {
			return v
		}
	}
	return ""
}

// auditEntry builds an audit entry from the current principal for job endpoints.
func auditEntryFor(r *http.Request) audit.Entry {
	p := principalFrom(r.Context())
	return audit.Entry{ActorUser: p.UserID, ActorRole: p.Role, IP: clientIP(r)}
}
