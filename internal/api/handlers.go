package api

import (
	"encoding/json"
	"errors"
	"net/http"
	"strconv"
	"time"

	"dams/internal/db"
	"dams/internal/platform/auth"
	"dams/internal/platform/httpx"
	"dams/internal/service/alerts"
	"dams/internal/service/export"
	"dams/internal/service/ingest"
	"dams/internal/service/ruleadmin"

	"github.com/go-chi/chi/v5"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"
)

type handlers struct{ d Deps }

// ---------- ingest ----------

func (h *handlers) IngestBatch(w http.ResponseWriter, r *http.Request) {
	p := auth.Must(w, r)
	if p == nil {
		return
	}
	var req ingest.BatchRequest
	if !httpx.DecodeJSON(w, r, &req, 8<<20) {
		return
	}
	res, err := h.d.Ingest.Ingest(r.Context(), p.OrgID, &req)
	if err != nil {
		var ve *ingest.ValidationError
		switch {
		case errors.As(err, &ve):
			httpx.Error(w, http.StatusBadRequest, ve.Error(), nil)
		case errors.Is(err, ingest.ErrConflict):
			httpx.Error(w, http.StatusConflict, err.Error(), nil)
		default:
			httpx.Error(w, http.StatusInternalServerError, err.Error(), nil)
		}
		return
	}
	httpx.JSON(w, http.StatusOK, res)
}

// ---------- rules ----------

func (h *handlers) CreateRule(w http.ResponseWriter, r *http.Request) {
	p := auth.Must(w, r)
	if p == nil {
		return
	}
	var in ruleadmin.CreateInput
	if !httpx.DecodeJSON(w, r, &in, 1<<20) {
		return
	}
	rv, err := h.d.Rules.Create(r.Context(), p.OrgID, ruleadmin.Actor{
		APIKeyID: p.APIKeyID, Label: p.Name,
	}, in)
	if err != nil {
		httpx.Error(w, http.StatusBadRequest, err.Error(), nil)
		return
	}
	httpx.JSON(w, http.StatusCreated, presentRule(rv))
}

func (h *handlers) ListRules(w http.ResponseWriter, r *http.Request) {
	p := auth.Must(w, r)
	if p == nil {
		return
	}
	rt := chi.URLParam(r, "ruleType")
	if rt != "frequency" && rt != "sensitive_hours" {
		httpx.Error(w, http.StatusBadRequest, "ruleType must be frequency or sensitive_hours", nil)
		return
	}
	q := db.New(h.d.Pool)
	rows, err := h.d.Rules.List(r.Context(), q, p.OrgID, rt)
	if err != nil {
		httpx.Error(w, http.StatusInternalServerError, err.Error(), nil)
		return
	}
	httpx.JSON(w, http.StatusOK, map[string]any{"rules": presentRules(rows)})
}

// ---------- alerts ----------

func (h *handlers) ListAlerts(w http.ResponseWriter, r *http.Request) {
	p := auth.Must(w, r)
	if p == nil {
		return
	}
	status := r.URL.Query().Get("status")
	if status != "" && !validStatus[status] {
		httpx.Error(w, http.StatusBadRequest, "invalid status", nil)
		return
	}
	limit, offset := parsePaging(r)
	q := db.New(h.d.Pool)
	rows, err := q.ListAlerts(r.Context(), db.ListAlertsParams{
		OrgID:        p.OrgID,
		FilterStatus: pgtype.Text{String: status, Valid: status != ""},
		Limit:        int32(limit),
		Offset:       int32(offset),
	})
	if err != nil {
		httpx.Error(w, http.StatusInternalServerError, err.Error(), nil)
		return
	}
	total, err := q.CountAlerts(r.Context(), db.CountAlertsParams{
		OrgID:        p.OrgID,
		FilterStatus: pgtype.Text{String: status, Valid: status != ""},
	})
	if err != nil {
		httpx.Error(w, http.StatusInternalServerError, err.Error(), nil)
		return
	}
	httpx.JSON(w, http.StatusOK, map[string]any{"alerts": presentAlerts(rows), "total": total})
}

var validStatus = map[string]bool{
	"pending": true, "investigating": true, "resolved": true, "false_positive": true,
}

func (h *handlers) GetAlert(w http.ResponseWriter, r *http.Request) {
	p := auth.Must(w, r)
	if p == nil {
		return
	}
	id, err := strconv.ParseInt(chi.URLParam(r, "id"), 10, 64)
	if err != nil {
		httpx.Error(w, http.StatusBadRequest, "bad alert id", nil)
		return
	}
	q := db.New(h.d.Pool)
	det, err := h.d.Alerts.Detail(r.Context(), q, p.OrgID, id, export.EventMap)
	if err != nil {
		writeAlertErr(w, err)
		return
	}
	httpx.JSON(w, http.StatusOK, map[string]any{
		"alert":    presentAlert(det.Alert),
		"history":  presentHistory(det.History),
		"evidence": presentEvidence(det.Evidence),
		"events":   det.Events,
	})
}

type transitionBody struct {
	ExpectedVersion int32  `json:"expected_version"`
	Status          string `json:"status"`
	Note            string `json:"note"`
}

func (h *handlers) TransitionAlert(w http.ResponseWriter, r *http.Request) {
	p := auth.Must(w, r)
	if p == nil {
		return
	}
	id, err := strconv.ParseInt(chi.URLParam(r, "id"), 10, 64)
	if err != nil {
		httpx.Error(w, http.StatusBadRequest, "bad alert id", nil)
		return
	}
	var body transitionBody
	if !httpx.DecodeJSON(w, r, &body, 1<<20) {
		return
	}
	if !validStatus[body.Status] {
		httpx.Error(w, http.StatusBadRequest, "invalid target status", nil)
		return
	}
	row, err := h.d.Alerts.Transition(r.Context(), p.OrgID, alerts.Actor{
		APIKeyID: p.APIKeyID, Label: p.Name,
	}, alerts.TransitionInput{
		AlertID: id, ExpectedVersion: body.ExpectedVersion,
		Status: body.Status, Note: body.Note,
	})
	if err != nil {
		writeAlertErr(w, err)
		return
	}
	httpx.JSON(w, http.StatusOK, presentAlert(row))
}

type recomputeBody struct {
	DbUser      string    `json:"db_user"`
	WindowStart time.Time `json:"window_start"`
}

func (h *handlers) Recompute(w http.ResponseWriter, r *http.Request) {
	p := auth.Must(w, r)
	if p == nil {
		return
	}
	var body recomputeBody
	if !httpx.DecodeJSON(w, r, &body, 1<<20) || body.DbUser == "" {
		httpx.Error(w, http.StatusBadRequest, "db_user is required", nil)
		return
	}
	out, err := h.d.Alerts.Recompute(r.Context(), p.OrgID, alerts.Actor{
		APIKeyID: p.APIKeyID, Label: p.Name,
	}, alerts.RecomputeInput{DbUser: body.DbUser, WindowStart: body.WindowStart})
	if err != nil {
		writeAlertErr(w, err)
		return
	}
	httpx.JSON(w, http.StatusOK, out)
}

func writeAlertErr(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, alerts.ErrNotFound):
		httpx.Error(w, http.StatusNotFound, err.Error(), nil)
	case errors.Is(err, alerts.ErrVersionConflict):
		httpx.Error(w, http.StatusConflict, err.Error(), nil)
	case errors.Is(err, alerts.ErrInvalidTransition):
		httpx.Error(w, http.StatusConflict, err.Error(), nil)
	default:
		httpx.Error(w, http.StatusInternalServerError, err.Error(), nil)
	}
}

// ---------- events export ----------

func (h *handlers) ListEvents(w http.ResponseWriter, r *http.Request) {
	p := auth.Must(w, r)
	if p == nil {
		return
	}
	q := db.New(h.d.Pool)
	limit, offset := parsePaging(r)
	params := db.ListEventsByOrgParams{
		OrgID:     p.OrgID,
		RowLimit:  int32(limit),
		RowOffset: int32(offset),
	}
	if v := r.URL.Query().Get("from"); v != "" {
		t, err := time.Parse(time.RFC3339, v)
		if err != nil {
			httpx.Error(w, http.StatusBadRequest, "bad 'from' timestamp", nil)
			return
		}
		params.FromTime = pgtype.Timestamptz{Time: t.UTC(), Valid: true}
	}
	if v := r.URL.Query().Get("to"); v != "" {
		t, err := time.Parse(time.RFC3339, v)
		if err != nil {
			httpx.Error(w, http.StatusBadRequest, "bad 'to' timestamp", nil)
			return
		}
		params.ToTime = pgtype.Timestamptz{Time: t.UTC(), Valid: true}
	}
	if v := r.URL.Query().Get("user"); v != "" {
		params.FilterUser = pgtype.Text{String: v, Valid: true}
	}
	if v := r.URL.Query().Get("table"); v != "" {
		params.FilterTable = pgtype.Text{String: v, Valid: true}
	}
	rows, err := q.ListEventsByOrg(r.Context(), params)
	if err != nil {
		httpx.Error(w, http.StatusInternalServerError, err.Error(), nil)
		return
	}
	masked := make([]export.MaskedEvent, 0, len(rows))
	for _, e := range rows {
		masked = append(masked, export.Event(e))
	}
	total, err := q.CountEventsByOrg(r.Context(), db.CountEventsByOrgParams{
		OrgID:    p.OrgID,
		FromTime: params.FromTime,
		ToTime:   params.ToTime,
	})
	if err != nil {
		httpx.Error(w, http.StatusInternalServerError, err.Error(), nil)
		return
	}
	httpx.JSON(w, http.StatusOK, map[string]any{"events": masked, "total": total})
}

// ---------- audit chain ----------

func (h *handlers) ListAuditEntries(w http.ResponseWriter, r *http.Request) {
	p := auth.Must(w, r)
	if p == nil {
		return
	}
	q := db.New(h.d.Pool)
	limit, _ := parsePaging(r)
	params := db.ListAuditEntriesParams{OrgID: p.OrgID, Limit: int32(limit)}
	if v := r.URL.Query().Get("from_seq"); v != "" {
		n, err := strconv.ParseInt(v, 10, 64)
		if err != nil {
			httpx.Error(w, http.StatusBadRequest, "bad from_seq", nil)
			return
		}
		params.FromSeq = pgtype.Int8{Int64: n, Valid: true}
	}
	if v := r.URL.Query().Get("to_seq"); v != "" {
		n, err := strconv.ParseInt(v, 10, 64)
		if err != nil {
			httpx.Error(w, http.StatusBadRequest, "bad to_seq", nil)
			return
		}
		params.ToSeq = pgtype.Int8{Int64: n, Valid: true}
	}
	rows, err := q.ListAuditEntries(r.Context(), params)
	if err != nil {
		httpx.Error(w, http.StatusInternalServerError, err.Error(), nil)
		return
	}
	httpx.JSON(w, http.StatusOK, map[string]any{"entries": presentAuditEntries(rows)})
}

func (h *handlers) VerifyChain(w http.ResponseWriter, r *http.Request) {
	p := auth.Must(w, r)
	if p == nil {
		return
	}
	rep, err := h.d.Chain.Verify(r.Context(), db.New(h.d.Pool), p.OrgID)
	if err != nil {
		httpx.Error(w, http.StatusInternalServerError, err.Error(), nil)
		return
	}
	status := http.StatusOK
	if !rep.OK {
		status = http.StatusConflict
	}
	httpx.JSON(w, status, rep)
}

// ---------- helpers ----------

func parsePaging(r *http.Request) (limit, offset int) {
	limit = 100
	if v := r.URL.Query().Get("limit"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 && n <= 1000 {
			limit = n
		}
	}
	if v := r.URL.Query().Get("offset"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n >= 0 {
			offset = n
		}
	}
	return
}

var _ = json.Marshal
var _ = pgx.ErrNoRows
