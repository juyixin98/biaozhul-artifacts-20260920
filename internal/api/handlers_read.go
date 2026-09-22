package api

import (
	"io"
	"net/http"
	"strconv"
	"time"

	"costlens/internal/auth"
	"costlens/internal/config"
	"costlens/internal/httpxx"
	"costlens/internal/service"
)

// ---------- Records / summaries / budgets / anomalies ----------

func (h *Handler) listRecords(w http.ResponseWriter, r *http.Request) {
	v, ok := h.visibility(w, r)
	if !ok {
		return
	}
	from, to, ok := dateRange(w, r, "2000-01-01", "2999-12-31")
	if !ok {
		return
	}
	limit, offset := parseLimitOffset(r)
	page, err := h.svc.ListRecords(r.Context(), v, service.RecordFilter{
		From: from, To: to, Currency: r.URL.Query().Get("currency"),
	}, limit, offset)
	if err != nil {
		httpxx.Error(w, 500, err.Error(), nil)
		return
	}
	httpxx.JSON(w, 200, page)
}

func (h *Handler) exportRecords(w http.ResponseWriter, r *http.Request) {
	v, ok := h.visibility(w, r)
	if !ok {
		return
	}
	from, to, ok := dateRange(w, r, "2000-01-01", "2999-12-31")
	if !ok {
		return
	}
	w.Header().Set("Content-Type", "text/csv; charset=utf-8")
	w.Header().Set("Content-Disposition", `attachment; filename="records.csv"`)
	if err := h.svc.StreamRecordsExport(r.Context(), v, service.RecordFilter{
		From: from, To: to, Currency: r.URL.Query().Get("currency"),
	}, w); err != nil {
		// Headers already sent; surface the failure through the CSV stream.
		_, _ = io.WriteString(w, "\n# export error: "+err.Error()+"\n")
	}
}

func (h *Handler) listDaily(w http.ResponseWriter, r *http.Request) {
	h.summaries(w, r, true)
}

func (h *Handler) listMonthly(w http.ResponseWriter, r *http.Request) {
	h.summaries(w, r, false)
}

func (h *Handler) summaries(w http.ResponseWriter, r *http.Request, daily bool) {
	v, ok := h.visibility(w, r)
	if !ok {
		return
	}
	from, to, ok := dateRange(w, r, "2000-01-01", "2999-12-31")
	if !ok {
		return
	}
	scope := r.URL.Query().Get("scope")
	if scope == "" {
		scope = "account"
	}
	if scope != "account" && scope != "cost_center" {
		httpxx.Error(w, 400, "scope must be account or cost_center", nil)
		return
	}
	if daily {
		rows, err := h.svc.ListDaily(r.Context(), v, scope, from, to)
		if err != nil {
			httpxx.Error(w, 500, err.Error(), nil)
			return
		}
		httpxx.JSON(w, 200, rows)
		return
	}
	rows, err := h.svc.ListMonthly(r.Context(), v, scope, from, to)
	if err != nil {
		httpxx.Error(w, 500, err.Error(), nil)
		return
	}
	httpxx.JSON(w, 200, rows)
}

func (h *Handler) listBudgets(w http.ResponseWriter, r *http.Request) {
	v, ok := h.visibility(w, r)
	if !ok {
		return
	}
	bs, err := h.svc.ListBudgets(r.Context(), v.CCIDs)
	if err != nil {
		httpxx.Error(w, 500, err.Error(), nil)
		return
	}
	httpxx.JSON(w, 200, bs)
}

func (h *Handler) setBudget(w http.ResponseWriter, r *http.Request) {
	var in service.SetBudgetInput
	if !decodeJSON(w, r, &in) {
		return
	}
	b, err := h.svc.SetBudget(r.Context(), in, auth.FromContext(r.Context()).ID)
	if err != nil {
		httpxx.Error(w, 400, err.Error(), nil)
		return
	}
	httpxx.JSON(w, 201, b)
}

func (h *Handler) listBudgetAlerts(w http.ResponseWriter, r *http.Request) {
	v, ok := h.visibility(w, r)
	if !ok {
		return
	}
	as, err := h.svc.ListBudgetAlerts(r.Context(), v.CCIDs)
	if err != nil {
		httpxx.Error(w, 500, err.Error(), nil)
		return
	}
	httpxx.JSON(w, 200, as)
}

func (h *Handler) listAnomalies(w http.ResponseWriter, r *http.Request) {
	v, ok := h.visibility(w, r)
	if !ok {
		return
	}
	only := r.URL.Query().Get("status") == "anomalous"
	as, err := h.svc.ListAnomalies(r.Context(), v, only)
	if err != nil {
		httpxx.Error(w, 500, err.Error(), nil)
		return
	}
	httpxx.JSON(w, 200, as)
}

func (h *Handler) listAnomalyVersions(w http.ResponseWriter, r *http.Request) {
	v, ok := h.visibility(w, r)
	if !ok {
		return
	}
	accountID, err := strconv.ParseInt(r.URL.Query().Get("account_id"), 10, 64)
	if err != nil {
		httpxx.Error(w, 400, "account_id is required", nil)
		return
	}
	if !canReadAccount(v, accountID) {
		httpxx.Error(w, 403, "not authorized for this account", nil)
		return
	}
	date := r.URL.Query().Get("date")
	currency := r.URL.Query().Get("currency")
	if date == "" || currency == "" {
		httpxx.Error(w, 400, "date and currency are required", nil)
		return
	}
	vs, err := h.svc.ListAnomalyVersions(r.Context(), accountID, date, currency)
	if err != nil {
		httpxx.Error(w, 500, err.Error(), nil)
		return
	}
	httpxx.JSON(w, 200, vs)
}

func canReadAccount(v service.Visibility, id int64) bool {
	if v.AccountIDs == nil {
		return true
	}
	for _, a := range v.AccountIDs {
		if a == id {
			return true
		}
	}
	return false
}

// ---------- shared helpers ----------

func (h *Handler) visibility(w http.ResponseWriter, r *http.Request) (service.Visibility, bool) {
	p := auth.FromContext(r.Context())
	v, err := h.svc.VisibilityFor(r.Context(), p)
	if err != nil {
		httpxx.Error(w, 500, err.Error(), nil)
		return v, false
	}
	return v, true
}

func parseLimitOffset(r *http.Request) (int32, int32) {
	return config.ParseLimitOffset(r.URL.Query().Get("limit"),
		r.URL.Query().Get("offset"))
}

func dateRange(w http.ResponseWriter, r *http.Request, defFrom, defTo string) (time.Time, time.Time, bool) {
	q := r.URL.Query()
	fromS := q.Get("from")
	if fromS == "" {
		fromS = defFrom
	}
	toS := q.Get("to")
	if toS == "" {
		toS = defTo
	}
	from, err := time.Parse("2006-01-02", fromS)
	if err != nil {
		httpxx.Error(w, 400, "from must be YYYY-MM-DD", nil)
		return time.Time{}, time.Time{}, false
	}
	to, err := time.Parse("2006-01-02", toS)
	if err != nil {
		httpxx.Error(w, 400, "to must be YYYY-MM-DD", nil)
		return time.Time{}, time.Time{}, false
	}
	if to.Before(from) {
		httpxx.Error(w, 400, "to must not be before from", nil)
		return time.Time{}, time.Time{}, false
	}
	return from, to, true
}
