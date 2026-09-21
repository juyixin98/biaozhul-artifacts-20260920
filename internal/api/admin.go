package api

import (
	"net/http"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/google/uuid"

	"github.com/clearsettle/clearsettle/internal/domain"
	"github.com/clearsettle/clearsettle/internal/service"
)

type createMerchantRequest struct {
	Name          string `json:"name"`
	FeeBPS        int    `json:"fee_bps,omitempty"`
	FeeFixedCents int64  `json:"fee_fixed_cents,omitempty"`
}

func (s *Server) createMerchant(w http.ResponseWriter, r *http.Request) {
	var req createMerchantRequest
	if err := decode(r, &req); err != nil {
		writeErr(w, domain.ErrValidation)
		return
	}
	out, err := s.svc.CreateMerchant(r.Context(), actorFrom(r), service.CreateMerchantInput{
		Name:          req.Name,
		FeeBPS:        req.FeeBPS,
		FeeFixedCents: req.FeeFixedCents,
	})
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{
		"merchant_id":     out.Merchant.ID.String(),
		"name":            out.Merchant.Name,
		"fee_bps":         out.Merchant.FeeBps,
		"fee_fixed_cents": out.Merchant.FeeFixedCents,
		"operator_key":    out.OperatorKey, // shown once
	})
}

func (s *Server) getMerchant(w http.ResponseWriter, r *http.Request) {
	id, err := uuid.Parse(chi.URLParam(r, "merchantID"))
	if err != nil {
		writeErr(w, domain.ErrValidation)
		return
	}
	m, err := s.svc.GetMerchant(r.Context(), actorFrom(r), id)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, merchantJSON(m))
}

func (s *Server) listMerchants(w http.ResponseWriter, r *http.Request) {
	rows, err := s.svc.ListMerchants(r.Context(), actorFrom(r))
	if err != nil {
		writeErr(w, err)
		return
	}
	out := make([]map[string]any, 0, len(rows))
	for _, m := range rows {
		out = append(out, merchantJSON(m))
	}
	writeJSON(w, http.StatusOK, map[string]any{"merchants": out})
}

type updateMerchantRequest struct {
	Name          *string `json:"name,omitempty"`
	FeeBPS        *int    `json:"fee_bps,omitempty"`
	FeeFixedCents *int64  `json:"fee_fixed_cents,omitempty"`
	Active        *bool   `json:"active,omitempty"`
}

func (s *Server) updateMerchant(w http.ResponseWriter, r *http.Request) {
	id, err := uuid.Parse(chi.URLParam(r, "merchantID"))
	if err != nil {
		writeErr(w, domain.ErrValidation)
		return
	}
	var req updateMerchantRequest
	if err := decode(r, &req); err != nil {
		writeErr(w, domain.ErrValidation)
		return
	}
	m, err := s.svc.UpdateMerchant(r.Context(), actorFrom(r), id, service.UpdateMerchantInput{
		Name:          req.Name,
		FeeBPS:        req.FeeBPS,
		FeeFixedCents: req.FeeFixedCents,
		Active:        req.Active,
	})
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, merchantJSON(m))
}

type issueKeyRequest struct {
	Role       string `json:"role"`
	MerchantID string `json:"merchant_id,omitempty"`
	Label      string `json:"label,omitempty"`
}

func (s *Server) issueKey(w http.ResponseWriter, r *http.Request) {
	var req issueKeyRequest
	if err := decode(r, &req); err != nil {
		writeErr(w, domain.ErrValidation)
		return
	}
	in := service.IssueKeyInput{Role: req.Role, Label: req.Label}
	if req.MerchantID != "" {
		mid, err := uuid.Parse(req.MerchantID)
		if err != nil {
			writeErr(w, domain.ErrValidation)
			return
		}
		in.MerchantID = &mid
	}
	k, err := s.svc.IssueAPIKey(r.Context(), actorFrom(r), in)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{
		"key_id":  k.KeyID.String(),
		"api_key": k.Key, // shown once
		"prefix":  k.Prefix,
		"role":    k.Role,
	})
}

func (s *Server) listKeys(w http.ResponseWriter, r *http.Request) {
	rows, err := s.svc.ListAPIKeys(r.Context(), actorFrom(r))
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"keys": rows})
}

func (s *Server) settle(w http.ResponseWriter, r *http.Request) {
	mid, err := uuid.Parse(chi.URLParam(r, "merchantID"))
	if err != nil {
		writeErr(w, domain.ErrValidation)
		return
	}
	date := r.URL.Query().Get("date")
	if date == "" {
		date = time.Now().UTC().Format("2006-01-02")
	}
	d, err := time.Parse("2006-01-02", date)
	if err != nil {
		writeErr(w, domain.ErrValidation)
		return
	}
	res, err := s.svc.SettleDay(r.Context(), actorFrom(r), mid, d)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"batch_id":        res.Batch.ID.String(),
		"batch_date":      res.Batch.BatchDate.Format("2006-01-02"),
		"status":          res.Batch.Status,
		"total_cents":     res.Batch.TotalCents,
		"fee_cents":       res.Batch.FeeCents,
		"net_cents":       res.Batch.NetCents,
		"items_settled":   res.ItemsSettled,
		"already_existed": res.AlreadyExisted,
	})
}

func (s *Server) reconcile(w http.ResponseWriter, r *http.Request) {
	mid, err := uuid.Parse(chi.URLParam(r, "merchantID"))
	if err != nil {
		writeErr(w, domain.ErrValidation)
		return
	}
	date := r.URL.Query().Get("date")
	if date == "" {
		date = time.Now().UTC().Format("2006-01-02")
	}
	d, err := time.Parse("2006-01-02", date)
	if err != nil {
		writeErr(w, domain.ErrValidation)
		return
	}
	res, err := s.svc.Reconcile(r.Context(), actorFrom(r), mid, d)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, reconJSON(res))
}

func (s *Server) listReconRuns(w http.ResponseWriter, r *http.Request) {
	mid, err := uuid.Parse(chi.URLParam(r, "merchantID"))
	if err != nil {
		writeErr(w, domain.ErrValidation)
		return
	}
	rows, err := s.svc.ListReconRuns(r.Context(), actorFrom(r), mid)
	if err != nil {
		writeErr(w, err)
		return
	}
	out := make([]map[string]any, 0, len(rows))
	for _, row := range rows {
		out = append(out, reconRunJSON(row))
	}
	writeJSON(w, http.StatusOK, map[string]any{"runs": out})
}

func (s *Server) getReconRun(w http.ResponseWriter, r *http.Request) {
	mid, err := uuid.Parse(chi.URLParam(r, "merchantID"))
	if err != nil {
		writeErr(w, domain.ErrValidation)
		return
	}
	date := chi.URLParam(r, "date")
	if _, err := time.Parse("2006-01-02", date); err != nil {
		writeErr(w, domain.ErrValidation)
		return
	}
	run, disc, err := s.svc.GetReconRun(r.Context(), actorFrom(r), mid, date)
	if err != nil {
		writeErr(w, err)
		return
	}
	out := reconRunJSON(run)
	discs := make([]map[string]any, 0, len(disc))
	for _, d := range disc {
		discs = append(discs, map[string]any{
			"id":             d.ID.String(),
			"kind":           d.Kind,
			"tx_id":          uuidPtrString(d.TxID),
			"expected_cents": d.ExpectedCents,
			"actual_cents":   d.ActualCents,
			"diff_cents":     d.DiffCents,
			"detail":         d.Detail,
		})
	}
	out["discrepancies"] = discs
	writeJSON(w, http.StatusOK, out)
}

func (s *Server) listBatches(w http.ResponseWriter, r *http.Request) {
	mid, err := uuid.Parse(chi.URLParam(r, "merchantID"))
	if err != nil {
		writeErr(w, domain.ErrValidation)
		return
	}
	rows, err := s.svc.ListBatches(r.Context(), actorFrom(r), mid)
	if err != nil {
		writeErr(w, err)
		return
	}
	out := make([]map[string]any, 0, len(rows))
	for _, b := range rows {
		out = append(out, batchJSON(b))
	}
	writeJSON(w, http.StatusOK, map[string]any{"batches": out})
}

func (s *Server) getBatch(w http.ResponseWriter, r *http.Request) {
	id, err := uuid.Parse(chi.URLParam(r, "batchID"))
	if err != nil {
		writeErr(w, domain.ErrValidation)
		return
	}
	b, items, err := s.svc.GetBatch(r.Context(), actorFrom(r), id)
	if err != nil {
		writeErr(w, err)
		return
	}
	out := batchJSON(b)
	itemViews := make([]map[string]any, 0, len(items))
	for _, it := range items {
		itemViews = append(itemViews, map[string]any{
			"payment_id":  it.PaymentID.String(),
			"gross_cents": it.GrossCents,
			"fee_cents":   it.FeeCents,
			"net_cents":   it.NetCents,
		})
	}
	out["items"] = itemViews
	writeJSON(w, http.StatusOK, out)
}

func (s *Server) balances(w http.ResponseWriter, r *http.Request) {
	mid, err := uuid.Parse(chi.URLParam(r, "merchantID"))
	if err != nil {
		writeErr(w, domain.ErrValidation)
		return
	}
	rows, err := s.svc.AccountBalances(r.Context(), actorFrom(r), mid)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"accounts": rows})
}

func (s *Server) getLedger(w http.ResponseWriter, r *http.Request) {
	refType := chi.URLParam(r, "refType")
	refID, err := uuid.Parse(chi.URLParam(r, "refID"))
	if err != nil {
		writeErr(w, domain.ErrValidation)
		return
	}
	entries, err := s.svc.ListLedgerEntries(r.Context(), actorFrom(r), refType, refID)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"entries": entries})
}

type statementRequest struct {
	MerchantID  string `json:"merchant_id"`
	RefID       string `json:"ref_id"`
	Kind        string `json:"kind"`
	AmountCents int64  `json:"amount_cents"`
	StmtDate    string `json:"stmt_date"`
}

func (s *Server) importStatement(w http.ResponseWriter, r *http.Request) {
	var req statementRequest
	if err := decode(r, &req); err != nil {
		writeErr(w, domain.ErrValidation)
		return
	}
	mid, err := uuid.Parse(req.MerchantID)
	if err != nil {
		writeErr(w, domain.ErrValidation)
		return
	}
	ref, err := uuid.Parse(req.RefID)
	if err != nil {
		writeErr(w, domain.ErrValidation)
		return
	}
	d, err := time.Parse("2006-01-02", req.StmtDate)
	if err != nil {
		writeErr(w, domain.ErrValidation)
		return
	}
	if err := s.svc.ImportStatementRow(r.Context(), actorFrom(r), mid, service.StatementInput{
		RefID: ref, Kind: req.Kind, AmountCents: req.AmountCents, StmtDate: d,
	}); err != nil {
		writeErr(w, err)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

type syncStatementsRequest struct {
	MerchantID string `json:"merchant_id"`
	AsOf       string `json:"as_of,omitempty"`
}

func (s *Server) syncStatements(w http.ResponseWriter, r *http.Request) {
	var req syncStatementsRequest
	if err := decode(r, &req); err != nil {
		writeErr(w, domain.ErrValidation)
		return
	}
	mid, err := uuid.Parse(req.MerchantID)
	if err != nil {
		writeErr(w, domain.ErrValidation)
		return
	}
	asOf := time.Now()
	if req.AsOf != "" {
		asOf, err = time.Parse("2006-01-02", req.AsOf)
		if err != nil {
			writeErr(w, domain.ErrValidation)
			return
		}
	}
	n, err := s.svc.SyncGatewayStatements(r.Context(), actorFrom(r), mid, asOf)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"rows_synced": n})
}

type correctionRequest struct {
	MerchantID  string `json:"merchant_id"`
	AccountCode string `json:"account_code"`
	AmountCents int64  `json:"amount_cents"`
	Reason      string `json:"reason"`
}

func (s *Server) postCorrection(w http.ResponseWriter, r *http.Request) {
	var req correctionRequest
	if err := decode(r, &req); err != nil {
		writeErr(w, domain.ErrValidation)
		return
	}
	mid, err := uuid.Parse(req.MerchantID)
	if err != nil {
		writeErr(w, domain.ErrValidation)
		return
	}
	ref, err := s.svc.PostCorrection(r.Context(), actorFrom(r), service.CorrectionInput{
		MerchantID:  mid,
		AccountCode: req.AccountCode,
		AmountCents: req.AmountCents,
		Reason:      req.Reason,
	})
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{"correction_ref": ref.String()})
}

func (s *Server) listAudit(w http.ResponseWriter, r *http.Request) {
	var mid *uuid.UUID
	if m := r.URL.Query().Get("merchant_id"); m != "" {
		parsed, err := uuid.Parse(m)
		if err != nil {
			writeErr(w, domain.ErrValidation)
			return
		}
		mid = &parsed
	}
	rows, err := s.svc.ListAuditEvents(r.Context(), actorFrom(r), mid,
		int32(atoiDefault(r.URL.Query().Get("limit"), 50)),
		int32(atoiDefault(r.URL.Query().Get("offset"), 0)))
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"events": rows})
}
