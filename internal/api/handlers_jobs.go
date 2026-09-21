package api

import (
	"encoding/json"
	"net/http"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgtype"

	"github.com/clearsettle/clearsettle/internal/store"
)

// POST /v1/jobs/settle  {"day":"2026-09-19"}  (optional; default: sweep eligible days)
func (d Deps) handleSettleNow(w http.ResponseWriter, r *http.Request) {
	p := principalFrom(r.Context())
	var body struct {
		Day string `json:"day"`
	}
	_ = json.NewDecoder(r.Body).Decode(&body)
	if body.Day != "" {
		day, err := time.ParseInLocation("2006-01-02", body.Day, time.UTC)
		if err != nil {
			writeError(w, http.StatusBadRequest, "bad_day", "day must be YYYY-MM-DD")
			return
		}
		res, err := d.Settle.SettleDay(r.Context(), *p.MerchantID, day, auditEntryFor(r))
		if err != nil {
			writeError(w, http.StatusInternalServerError, "settle_failed", err.Error())
			return
		}
		writeJSON(w, http.StatusOK, res)
		return
	}
	results, err := d.Settle.RunSweep(r.Context(), time.Now(), d.SettleHorizon, p.MerchantID, auditEntryFor(r))
	if err != nil {
		writeError(w, http.StatusInternalServerError, "settle_failed", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"settled": results})
}

// POST /v1/jobs/reconcile/{date}
func (d Deps) handleReconcileNow(w http.ResponseWriter, r *http.Request) {
	p := principalFrom(r.Context())
	date := chi.URLParam(r, "date")
	var day time.Time
	if date == "" || date == "yesterday" {
		day = time.Now().UTC().Add(-24 * time.Hour)
	} else {
		var err error
		day, err = time.ParseInLocation("2006-01-02", date, time.UTC)
		if err != nil {
			writeError(w, http.StatusBadRequest, "bad_day", "date must be YYYY-MM-DD")
			return
		}
	}
	res, err := d.Recon.Run(r.Context(), *p.MerchantID, day, auditEntryFor(r))
	if err != nil {
		writeError(w, http.StatusInternalServerError, "recon_failed", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, res)
}

// ------------------------------------------------------------- settlements

func settlementJSON(b store.SettlementBatch) map[string]any {
	out := map[string]any{
		"id":             b.ID.String(),
		"merchant_id":    b.MerchantID.String(),
		"batch_date":     b.BatchDate.Time.Format("2006-01-02"),
		"status":         b.Status,
		"gross_captured": b.GrossCaptured,
		"total_fees":     b.TotalFees,
		"total_refunds":  b.TotalRefunds,
		"refunded_fees":  b.RefundedFees,
		"net_amount":     b.NetAmount,
		"payment_count":  b.PaymentCount,
		"started_at":     b.StartedAt.Time.Format(time.RFC3339),
	}
	if b.CompletedAt.Valid {
		out["completed_at"] = b.CompletedAt.Time.Format(time.RFC3339)
	}
	return out
}

func (d Deps) handleListSettlements(w http.ResponseWriter, r *http.Request) {
	p := principalFrom(r.Context())
	limit, offset := pagination(r)
	if p.IsAuditor() {
		rows, err := d.Q.ListAllSettlementBatches(r.Context(), store.ListAllSettlementBatchesParams{
			Limit: limit, Offset: offset,
		})
		if err != nil {
			writeError(w, http.StatusInternalServerError, "internal", err.Error())
			return
		}
		filter := r.URL.Query().Get("merchant_id")
		out := make([]map[string]any, 0, len(rows))
		for _, b := range rows {
			if filter != "" && b.MerchantID.String() != filter {
				continue
			}
			m := settlementJSON(store.SettlementBatch{
				ID: b.ID, MerchantID: b.MerchantID, BatchDate: b.BatchDate,
				Status: b.Status, GrossCaptured: b.GrossCaptured, TotalFees: b.TotalFees,
				TotalRefunds: b.TotalRefunds, RefundedFees: b.RefundedFees,
				NetAmount: b.NetAmount, PaymentCount: b.PaymentCount,
				StartedAt: b.StartedAt, CompletedAt: b.CompletedAt,
			})
			m["merchant_name"] = b.MerchantName
			out = append(out, m)
		}
		writeJSON(w, http.StatusOK, map[string]any{"settlements": out, "limit": limit, "offset": offset})
		return
	}
	rows, err := d.Q.ListSettlementBatches(r.Context(), store.ListSettlementBatchesParams{
		MerchantID: *p.MerchantID, Limit: limit, Offset: offset,
	})
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	out := make([]map[string]any, 0, len(rows))
	for _, b := range rows {
		out = append(out, settlementJSON(b))
	}
	writeJSON(w, http.StatusOK, map[string]any{"settlements": out, "limit": limit, "offset": offset})
}

func (d Deps) handleGetSettlement(w http.ResponseWriter, r *http.Request) {
	p := principalFrom(r.Context())
	id, err := uuid.Parse(chi.URLParam(r, "id"))
	if err != nil {
		writeError(w, http.StatusBadRequest, "bad_id", "batch id must be a UUID")
		return
	}
	b, err := d.Q.GetSettlementBatch(r.Context(), id)
	if err != nil || (!p.IsAuditor() && b.MerchantID != *p.MerchantID) {
		writeError(w, http.StatusNotFound, "not_found", "settlement not found")
		return
	}
	payRows, err := d.Q.ListPaymentsInBatch(r.Context(), &b.ID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	payments := make([]string, 0, len(payRows))
	for _, pr := range payRows {
		payments = append(payments, pr.ID.String())
	}
	body := settlementJSON(b)
	body["payment_ids"] = payments
	writeJSON(w, http.StatusOK, body)
}

// --------------------------------------------------------- reconciliations

func reconRunJSON(run store.ReconciliationRun, summary []byte) map[string]any {
	out := map[string]any{
		"id":          run.ID.String(),
		"merchant_id": run.MerchantID.String(),
		"run_date":    run.RunDate.Time.Format("2006-01-02"),
		"status":      run.Status,
		"started_at":  run.StartedAt.Time.Format(time.RFC3339),
		"summary":     json.RawMessage(summary),
	}
	if run.CompletedAt.Valid {
		out["completed_at"] = run.CompletedAt.Time.Format(time.RFC3339)
	}
	return out
}

func (d Deps) handleListRecon(w http.ResponseWriter, r *http.Request) {
	p := principalFrom(r.Context())
	limit, offset := pagination(r)
	if p.IsAuditor() {
		rows, err := d.Q.ListAllReconRuns(r.Context(), store.ListAllReconRunsParams{
			Limit: limit, Offset: offset,
		})
		if err != nil {
			writeError(w, http.StatusInternalServerError, "internal", err.Error())
			return
		}
		filter := r.URL.Query().Get("merchant_id")
		out := make([]map[string]any, 0, len(rows))
		for _, run := range rows {
			if filter != "" && run.MerchantID.String() != filter {
				continue
			}
			m := reconRunJSON(store.ReconciliationRun{
				ID: run.ID, MerchantID: run.MerchantID, RunDate: run.RunDate,
				Status: run.Status, StartedAt: run.StartedAt,
				CompletedAt: run.CompletedAt, DetailSummary: run.DetailSummary,
			}, run.DetailSummary)
			m["merchant_name"] = run.MerchantName
			out = append(out, m)
		}
		writeJSON(w, http.StatusOK, map[string]any{"reconciliations": out, "limit": limit, "offset": offset})
		return
	}
	rows, err := d.Q.ListReconRunsForMerchant(r.Context(), store.ListReconRunsForMerchantParams{
		MerchantID: *p.MerchantID, Limit: limit, Offset: offset,
	})
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	out := make([]map[string]any, 0, len(rows))
	for _, run := range rows {
		out = append(out, reconRunJSON(run, run.DetailSummary))
	}
	writeJSON(w, http.StatusOK, map[string]any{"reconciliations": out, "limit": limit, "offset": offset})
}

func (d Deps) handleGetRecon(w http.ResponseWriter, r *http.Request) {
	date := chi.URLParam(r, "date")
	day, err := time.ParseInLocation("2006-01-02", date, time.UTC)
	if err != nil {
		writeError(w, http.StatusBadRequest, "bad_day", "date must be YYYY-MM-DD")
		return
	}
	mid, ok := d.scopeMerchant(w, r)
	if !ok {
		return
	}
	if mid == nil {
		writeError(w, http.StatusBadRequest, "merchant_required", "auditors must pass ?merchant_id=")
		return
	}
	run, err := d.Q.GetReconRun(r.Context(), store.GetReconRunParams{
		MerchantID: *mid, RunDate: dateArg(day),
	})
	if err != nil {
		writeError(w, http.StatusNotFound, "not_found", "no reconciliation for that date")
		return
	}
	items, err := d.Q.ListReconItems(r.Context(), run.ID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	out := reconRunJSON(run, run.DetailSummary)
	outItems := make([]map[string]any, 0, len(items))
	for _, it := range items {
		outItems = append(outItems, map[string]any{
			"check": it.CheckName, "expected": it.Expected, "actual": it.Actual,
			"difference": it.Difference, "severity": it.Severity, "detail": it.Detail.String,
		})
	}
	out["items"] = outItems
	writeJSON(w, http.StatusOK, out)
}

func dateArg(t time.Time) pgtype.Date {
	t = t.UTC()
	return pgtype.Date{Time: time.Date(t.Year(), t.Month(), t.Day(), 0, 0, 0, 0, time.UTC), Valid: true}
}
