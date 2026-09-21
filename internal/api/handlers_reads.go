package api

import (
	"net/http"
	"strconv"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/clearsettle/clearsettle/internal/ledger"
	"github.com/clearsettle/clearsettle/internal/payments"
	"github.com/clearsettle/clearsettle/internal/store"
)

func pagination(r *http.Request) (limit, offset int32) {
	limit = 50
	if v := r.URL.Query().Get("limit"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 && n <= 500 {
			limit = int32(n)
		}
	}
	if v := r.URL.Query().Get("offset"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n >= 0 {
			offset = int32(n)
		}
	}
	return
}

// scopeMerchant resolves which merchant the request may touch: the caller's own
// merchant for operators/API keys; an explicit ?merchant_id= (or all) for auditors.
func (d Deps) scopeMerchant(w http.ResponseWriter, r *http.Request) (*uuid.UUID, bool) {
	p := principalFrom(r.Context())
	if !p.IsAuditor() {
		if p.MerchantID == nil {
			writeError(w, http.StatusForbidden, "no_merchant", "no merchant scope")
			return nil, false
		}
		return p.MerchantID, true
	}
	if raw := r.URL.Query().Get("merchant_id"); raw != "" {
		id, err := uuid.Parse(raw)
		if err != nil {
			writeError(w, http.StatusBadRequest, "bad_merchant_id", "merchant_id must be a UUID")
			return nil, false
		}
		return &id, true
	}
	return nil, true // auditor, unscoped
}

func (d Deps) handleGetPayment(w http.ResponseWriter, r *http.Request) {
	p := principalFrom(r.Context())
	id, err := uuid.Parse(chi.URLParam(r, "id"))
	if err != nil {
		writeError(w, http.StatusBadRequest, "bad_id", "payment id must be a UUID")
		return
	}
	var row store.Payment
	if p.IsAuditor() {
		row, err = d.Q.GetPaymentAnyMerchant(r.Context(), id)
	} else {
		row, err = d.Q.GetPayment(r.Context(), store.GetPaymentParams{ID: id, MerchantID: *p.MerchantID})
	}
	if err != nil {
		writeError(w, http.StatusNotFound, "not_found", "payment not found")
		return
	}
	writeJSON(w, http.StatusOK, payments.EncodePayment(row))
}

func (d Deps) handleListPayments(w http.ResponseWriter, r *http.Request) {
	limit, offset := pagination(r)
	mid, ok := d.scopeMerchant(w, r)
	if !ok {
		return
	}
	rows, err := d.Q.ListPaymentsAnyMerchant(r.Context(), store.ListPaymentsAnyMerchantParams{
		MerchantID: mid, RowLimit: limit, RowOffset: offset,
	})
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	out := make([]payments.PaymentResponse, 0, len(rows))
	for _, row := range rows {
		out = append(out, payments.EncodePayment(row))
	}
	writeJSON(w, http.StatusOK, map[string]any{"payments": out, "limit": limit, "offset": offset})
}

func (d Deps) handleListRefunds(w http.ResponseWriter, r *http.Request) {
	p := principalFrom(r.Context())
	id, err := uuid.Parse(chi.URLParam(r, "id"))
	if err != nil {
		writeError(w, http.StatusBadRequest, "bad_id", "payment id must be a UUID")
		return
	}
	var rows []store.Refund
	if p.IsAuditor() {
		rows, err = d.Q.ListRefundsForPaymentAnyMerchant(r.Context(), id)
	} else {
		rows, err = d.Q.ListRefundsForPayment(r.Context(), store.ListRefundsForPaymentParams{
			PaymentID: id, MerchantID: *p.MerchantID,
		})
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	out := make([]payments.RefundResponse, 0, len(rows))
	for _, row := range rows {
		out = append(out, payments.EncodeRefund(row))
	}
	writeJSON(w, http.StatusOK, map[string]any{"refunds": out})
}

func (d Deps) handleBalance(w http.ResponseWriter, r *http.Request) {
	mid, ok := d.scopeMerchant(w, r)
	if !ok {
		return
	}
	if mid == nil {
		writeError(w, http.StatusBadRequest, "merchant_required", "auditors must pass ?merchant_id=")
		return
	}
	acct, err := d.Q.GetLedgerAccountByCode(r.Context(), ledger.PayableCode(*mid))
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	bal, err := d.Q.AccountBalance(r.Context(), acct.ID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	// Payable is stored with a negative sign (liability); report amount owed as
	// a positive number.
	writeJSON(w, http.StatusOK, map[string]any{
		"merchant_id": mid.String(),
		"payable":     -bal,
		"as_of":       time.Now().UTC().Format(time.RFC3339),
	})
}

var _ = pgx.ErrNoRows
