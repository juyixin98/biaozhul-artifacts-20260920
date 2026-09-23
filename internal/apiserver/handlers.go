package apiserver

import (
	"encoding/json"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"

	"costlens/internal/auth"
	"costlens/internal/service"

	"github.com/go-chi/chi/v5"
)

func (s *Server) listOrganizations(w http.ResponseWriter, r *http.Request) {
	p, _ := auth.FromContext(r.Context())
	orgs, err := s.svc.ListOrganizations(r.Context(), p)
	if err != nil {
		writeServiceError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"organizations": list(orgs)})
}

func (s *Server) listCostCenters(w http.ResponseWriter, r *http.Request) {
	p, _ := auth.FromContext(r.Context())
	rows, err := s.svc.ListCostCenters(r.Context(), p, orgIDsFromQuery(r))
	if err != nil {
		writeServiceError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"cost_centers": list(rows)})
}

func (s *Server) listAccounts(w http.ResponseWriter, r *http.Request) {
	p, _ := auth.FromContext(r.Context())
	rows, err := s.svc.ListAccounts(r.Context(), p, orgIDsFromQuery(r))
	if err != nil {
		writeServiceError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"accounts": list(rows)})
}

func (s *Server) createImport(w http.ResponseWriter, r *http.Request) {
	p, _ := auth.FromContext(r.Context())
	if !p.CanImport() {
		writeError(w, http.StatusForbidden, "forbidden", "role may not import bills")
		return
	}
	orgID := chi.URLParam(r, "org_id")
	if !p.CanAccessOrg(orgID) {
		writeError(w, http.StatusForbidden, "forbidden_org", "organization not in user scope")
		return
	}

	filename, body, err := readUpload(r)
	if err != nil {
		writeError(w, http.StatusBadRequest, "bad_upload", err.Error())
		return
	}
	defer body.Close()

	res, err := s.svc.Import(r.Context(), p, orgID, filename, body)
	if err != nil {
		writeServiceError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, res)
}

func (s *Server) listImports(w http.ResponseWriter, r *http.Request) {
	p, _ := auth.FromContext(r.Context())
	limit, offset := pagination(r)
	rows, total, err := s.svc.ListImports(r.Context(), p, orgIDsFromQuery(r), limit, offset)
	if err != nil {
		writeServiceError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"imports": list(rows), "total": total})
}

// ---- costs / export ----

func (s *Server) listCosts(w http.ResponseWriter, r *http.Request) {
	p, _ := auth.FromContext(r.Context())
	f, ferr := filtersFromQuery(r)
	if ferr != nil {
		writeError(w, http.StatusBadRequest, "invalid_filter", ferr.Error())
		return
	}
	rows, total, err := s.svc.ListCosts(r.Context(), p, orgIDsFromQuery(r), f)
	if err != nil {
		writeServiceError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"costs": list(rows), "total": total})
}

func (s *Server) exportCosts(w http.ResponseWriter, r *http.Request) {
	p, _ := auth.FromContext(r.Context())
	f, ferr := filtersFromQuery(r)
	if ferr != nil {
		writeError(w, http.StatusBadRequest, "invalid_filter", ferr.Error())
		return
	}
	w.Header().Set("Content-Type", "text/csv; charset=utf-8")
	w.Header().Set("Content-Disposition", `attachment; filename="costs.csv"`)
	if err := s.svc.ExportCosts(r.Context(), p, orgIDsFromQuery(r), f, w); err != nil {
		writeServiceError(w, err)
	}
}

// ---- summaries ----

func (s *Server) dailyAccount(w http.ResponseWriter, r *http.Request) {
	p, _ := auth.FromContext(r.Context())
	f, ferr := filtersFromQuery(r)
	if ferr != nil {
		writeError(w, http.StatusBadRequest, "invalid_filter", ferr.Error())
		return
	}
	rows, err := s.svc.DailyAccount(r.Context(), p, orgIDsFromQuery(r), f)
	if err != nil {
		writeServiceError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"summaries": list(rows)})
}

func (s *Server) monthlyAccount(w http.ResponseWriter, r *http.Request) {
	p, _ := auth.FromContext(r.Context())
	f, ferr := filtersFromQuery(r)
	if ferr != nil {
		writeError(w, http.StatusBadRequest, "invalid_filter", ferr.Error())
		return
	}
	rows, err := s.svc.MonthlyAccount(r.Context(), p, orgIDsFromQuery(r), f)
	if err != nil {
		writeServiceError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"summaries": list(rows)})
}

func (s *Server) dailyCostCenter(w http.ResponseWriter, r *http.Request) {
	p, _ := auth.FromContext(r.Context())
	f, ferr := filtersFromQuery(r)
	if ferr != nil {
		writeError(w, http.StatusBadRequest, "invalid_filter", ferr.Error())
		return
	}
	rows, err := s.svc.DailyCostCenter(r.Context(), p, orgIDsFromQuery(r), f)
	if err != nil {
		writeServiceError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"summaries": list(rows)})
}

func (s *Server) monthlyCostCenter(w http.ResponseWriter, r *http.Request) {
	p, _ := auth.FromContext(r.Context())
	f, ferr := filtersFromQuery(r)
	if ferr != nil {
		writeError(w, http.StatusBadRequest, "invalid_filter", ferr.Error())
		return
	}
	rows, err := s.svc.MonthlyCostCenter(r.Context(), p, orgIDsFromQuery(r), f)
	if err != nil {
		writeServiceError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"summaries": list(rows)})
}

// ---- budgets ----

func (s *Server) putBudget(w http.ResponseWriter, r *http.Request) {
	p, _ := auth.FromContext(r.Context())
	var in service.CreateBudgetInput
	if err := json.NewDecoder(r.Body).Decode(&in); err != nil {
		writeError(w, http.StatusBadRequest, "invalid_json", err.Error())
		return
	}
	b, err := s.svc.CreateBudget(r.Context(), p, in)
	if err != nil {
		writeServiceError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, b)
}

func (s *Server) listBudgets(w http.ResponseWriter, r *http.Request) {
	p, _ := auth.FromContext(r.Context())
	f, ferr := filtersFromQuery(r)
	if ferr != nil {
		writeError(w, http.StatusBadRequest, "invalid_filter", ferr.Error())
		return
	}
	rows, err := s.svc.ListBudgets(r.Context(), p, orgIDsFromQuery(r), f)
	if err != nil {
		writeServiceError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"budgets": list(rows)})
}

func (s *Server) listBudgetAlerts(w http.ResponseWriter, r *http.Request) {
	p, _ := auth.FromContext(r.Context())
	f, ferr := filtersFromQuery(r)
	if ferr != nil {
		writeError(w, http.StatusBadRequest, "invalid_filter", ferr.Error())
		return
	}
	rows, err := s.svc.ListBudgetAlerts(r.Context(), p, orgIDsFromQuery(r), f)
	if err != nil {
		writeServiceError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"budget_alerts": list(rows)})
}

// ---- anomalies ----

func (s *Server) listAnomalies(w http.ResponseWriter, r *http.Request) {
	p, _ := auth.FromContext(r.Context())
	f, ferr := filtersFromQuery(r)
	if ferr != nil {
		writeError(w, http.StatusBadRequest, "invalid_filter", ferr.Error())
		return
	}
	rows, err := s.svc.ListLatestAnomalies(r.Context(), p, orgIDsFromQuery(r), f)
	if err != nil {
		writeServiceError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"anomalies": list(rows)})
}

func (s *Server) listAnomalyHistory(w http.ResponseWriter, r *http.Request) {
	p, _ := auth.FromContext(r.Context())
	f, ferr := filtersFromQuery(r)
	if ferr != nil {
		writeError(w, http.StatusBadRequest, "invalid_filter", ferr.Error())
		return
	}
	rows, err := s.svc.ListAnomalyHistory(r.Context(), p, orgIDsFromQuery(r), f)
	if err != nil {
		writeServiceError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"evaluations": list(rows)})
}

// ---- admin ----

func (s *Server) rebuild(w http.ResponseWriter, r *http.Request) {
	p, _ := auth.FromContext(r.Context())
	if p.Role != auth.RoleAdmin {
		writeError(w, http.StatusForbidden, "forbidden", "rebuild requires admin")
		return
	}
	n, err := s.svc.Rebuild(r.Context())
	if err != nil {
		writeServiceError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"status": "ok", "accounts_recomputed": n})
}

// ---- upload + query parsing ----

func readUpload(r *http.Request) (string, io.ReadCloser, error) {
	ct := r.Header.Get("Content-Type")
	if strings.HasPrefix(ct, "multipart/form-data") {
		if err := r.ParseMultipartForm(25 << 20); err != nil {
			return "", nil, err
		}
		file, hdr, err := r.FormFile("file")
		if err != nil {
			return "", nil, err
		}
		return hdr.Filename, file, nil
	}
	// Raw body (text/csv or application/octet-stream).
	name := r.URL.Query().Get("filename")
	if name == "" {
		name = "upload.csv"
	}
	return name, io.NopCloser(r.Body), nil
}

func orgIDsFromQuery(r *http.Request) []string {
	var out []string
	for _, s := range r.URL.Query()["org_id"] {
		for _, part := range strings.Split(s, ",") {
			if p := strings.TrimSpace(part); p != "" {
				out = append(out, p)
			}
		}
	}
	return out
}

func pagination(r *http.Request) (int32, int32) {
	parse := func(k string, def, max int32) int32 {
		v := r.URL.Query().Get(k)
		if v == "" {
			return def
		}
		n, err := strconv.Atoi(v)
		if err != nil || n < 0 {
			return def
		}
		if int32(n) > max {
			return max
		}
		return int32(n)
	}
	return parse("limit", 1000, 5000), parse("offset", 0, 1_000_000)
}

func filtersFromQuery(r *http.Request) (service.Filters, error) {
	q := r.URL.Query()
	f := service.Filters{
		AccountID:    q.Get("account_id"),
		CostCenterID: q.Get("cost_center_id"),
		Currency:     strings.ToUpper(q.Get("currency")),
	}
	var err error
	parseDate := func(k string) (time.Time, error) {
		return time.Parse("2006-01-02", q.Get(k))
	}
	if v := q.Get("date"); v != "" {
		if f.DateEq, err = parseDate("date"); err != nil {
			return f, errBad("date")
		}
	}
	if v := q.Get("from"); v != "" {
		if f.DateFrom, err = parseDate("from"); err != nil {
			return f, errBad("from")
		}
	}
	if v := q.Get("to"); v != "" {
		if f.DateTo, err = parseDate("to"); err != nil {
			return f, errBad("to")
		}
	}
	if v := q.Get("period"); v != "" {
		if f.Period, err = parseDate("period"); err != nil {
			return f, errBad("period")
		}
	}
	if v := q.Get("currency"); v != "" {
		if len(v) != 3 || strings.ToUpper(v) != v {
			return f, &simpleErr{"currency must be 3 uppercase letters"}
		}
	}
	f.Limit, f.Offset = pagination(r)
	return f, nil
}

type simpleErr struct{ msg string }

func (e *simpleErr) Error() string { return e.msg }

func errBad(field string) error {
	return &simpleErr{field + " must be YYYY-MM-DD"}
}
