// Package httpapi exposes the REST protocol with chi.
package httpapi

import (
	"encoding/json"
	"errors"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"

	"twap/internal/service"
	"twap/internal/storage"
	"twap/internal/twap"
)

// Server holds dependencies.
type Server struct {
	svc        *service.Service
	adminToken string
}

// NewRouter builds the chi router.
func NewRouter(svc *service.Service, adminToken string) http.Handler {
	s := &Server{svc: svc, adminToken: adminToken}
	r := chi.NewRouter()
	r.Use(middleware.RequestID)
	r.Use(middleware.RealIP)
	r.Use(middleware.Logger)
	r.Use(middleware.Recoverer)
	r.Use(middleware.Timeout(30 * time.Second))

	r.Get("/healthz", s.health)

	r.Route("/v1", func(r chi.Router) {
		r.Post("/samples", s.ingest)
		r.Get("/windows/latest", s.readWindow) // ?at=<unix micros|RFC3339>
		r.Get("/windows/{start}/versions/{version}", s.readVersion)
		r.Get("/range", s.readRange) // ?start=&end=
		r.Get("/windows", s.listWindows)
		r.Post("/admin/recompute", s.fullRecompute)
	})
	return r
}

func (s *Server) health(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

// ingestReq accepts one sample or a batch.
type ingestReq struct {
	TS     json.RawMessage `json:"ts"`
	Price  int64           `json:"price"`
	Source string          `json:"source"`
	Batch  []ingestReqItem `json:"batch"`
}

type ingestReqItem struct {
	TS     json.RawMessage `json:"ts"`
	Price  int64           `json:"price"`
	Source string          `json:"source"`
}

func (s *Server) ingest(w http.ResponseWriter, r *http.Request) {
	var req ingestReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
		return
	}

	items := req.Batch
	if items == nil {
		items = []ingestReqItem{{TS: req.TS, Price: req.Price, Source: req.Source}}
	}
	if len(items) == 0 {
		writeErr(w, http.StatusBadRequest, "no samples provided")
		return
	}

	type itemResp struct {
		Index  int                   `json:"index"`
		Result *service.IngestResult `json:"result,omitempty"`
		Error  string                `json:"error,omitempty"`
		Status int                   `json:"status"`
	}
	resp := struct {
		Accepted int        `json:"accepted"`
		Rejected int        `json:"rejected"`
		Items    []itemResp `json:"items"`
	}{}

	for i, it := range items {
		ts, err := parseTimestamp(it.TS)
		if err != nil {
			resp.Rejected++
			resp.Items = append(resp.Items, itemResp{Index: i, Error: err.Error(), Status: http.StatusBadRequest})
			continue
		}
		if strings.TrimSpace(it.Source) == "" {
			resp.Rejected++
			resp.Items = append(resp.Items, itemResp{Index: i, Error: "source is required", Status: http.StatusBadRequest})
			continue
		}
		res, err := s.svc.Ingest(r.Context(), twap.Sample{
			TS: ts, Price: it.Price, Source: it.Source,
		})
		if err != nil {
			status := http.StatusInternalServerError
			switch {
			case errors.Is(err, service.ErrFutureSample):
				status = http.StatusUnprocessableEntity
			case errors.Is(err, service.ErrTooLate):
				status = http.StatusUnprocessableEntity
			}
			resp.Rejected++
			resp.Items = append(resp.Items, itemResp{Index: i, Error: err.Error(), Status: status})
			continue
		}
		resp.Accepted++
		resp.Items = append(resp.Items, itemResp{Index: i, Result: res, Status: http.StatusOK})
	}
	writeJSON(w, http.StatusOK, resp)
}

func (s *Server) readWindow(w http.ResponseWriter, r *http.Request) {
	at := time.Now().UnixMicro()
	if raw := r.URL.Query().Get("at"); raw != "" {
		v, err := parseTimestampStr(raw)
		if err != nil {
			writeErr(w, http.StatusBadRequest, err.Error())
			return
		}
		at = v
	}
	verify := r.URL.Query().Get("verify_signature") == "1"
	view, err := s.svc.ReadWindow(r.Context(), at, verify)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, view)
}

func (s *Server) readVersion(w http.ResponseWriter, r *http.Request) {
	start, err := strconv.ParseInt(chi.URLParam(r, "start"), 10, 64)
	if err != nil {
		writeErr(w, http.StatusBadRequest, "window start must be unix microseconds")
		return
	}
	version, err := strconv.Atoi(chi.URLParam(r, "version"))
	if err != nil || version < 1 {
		writeErr(w, http.StatusBadRequest, "version must be a positive integer")
		return
	}
	verify := r.URL.Query().Get("verify_signature") == "1"
	view, err := s.svc.ReadVersion(r.Context(), start, version, verify)
	if errors.Is(err, storage.ErrNotFound) {
		writeErr(w, http.StatusNotFound, "version not found")
		return
	}
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, view)
}

func (s *Server) readRange(w http.ResponseWriter, r *http.Request) {
	start, err := parseTimestampStr(r.URL.Query().Get("start"))
	if err != nil {
		writeErr(w, http.StatusBadRequest, "start: "+err.Error())
		return
	}
	end, err := parseTimestampStr(r.URL.Query().Get("end"))
	if err != nil {
		writeErr(w, http.StatusBadRequest, "end: "+err.Error())
		return
	}
	res, err := s.svc.ReadRange(r.Context(), start, end)
	if err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, res)
}

func (s *Server) listWindows(w http.ResponseWriter, r *http.Request) {
	limit := 50
	if l := r.URL.Query().Get("limit"); l != "" {
		if n, err := strconv.Atoi(l); err == nil && n > 0 && n <= 500 {
			limit = n
		}
	}
	starts, err := s.svc.ListWindows(r.Context(), limit)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"window_starts": starts})
}

func (s *Server) fullRecompute(w http.ResponseWriter, r *http.Request) {
	if !s.checkAdmin(w, r) {
		return
	}
	diffs, checked, err := s.svc.FullRecompute(r.Context())
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"checked_windows": checked,
		"new_versions":    len(diffs),
		"diffs":           diffs,
	})
}

func (s *Server) checkAdmin(w http.ResponseWriter, r *http.Request) bool {
	if s.adminToken == "" {
		writeErr(w, http.StatusServiceUnavailable, "admin endpoint disabled: no ADMIN_TOKEN configured")
		return false
	}
	token := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
	if token == "" {
		token = r.Header.Get("X-Admin-Token")
	}
	if token != s.adminToken {
		writeErr(w, http.StatusUnauthorized, "invalid admin token")
		return false
	}
	return true
}

// parseTimestamp accepts a JSON number (unix microseconds) or a JSON
// string (RFC3339Nano), e.g. "2026-09-22T12:00:00Z".
func parseTimestamp(raw json.RawMessage) (int64, error) {
	if len(raw) == 0 {
		return 0, errors.New("ts is required")
	}
	if raw[0] == '"' {
		var s string
		if err := json.Unmarshal(raw, &s); err != nil {
			return 0, errors.New("ts: invalid string")
		}
		return parseTimestampStr(s)
	}
	var n int64
	if err := json.Unmarshal(raw, &n); err != nil {
		return 0, errors.New("ts must be unix microseconds (integer) or RFC3339 string")
	}
	return n, nil
}

func parseTimestampStr(s string) (int64, error) {
	if s == "" {
		return 0, errors.New("timestamp is required")
	}
	if n, err := strconv.ParseInt(s, 10, 64); err == nil {
		return n, nil
	}
	t, err := time.Parse(time.RFC3339Nano, s)
	if err != nil {
		return 0, errors.New("timestamp must be unix microseconds or RFC3339 (e.g. 2026-09-22T12:00:00Z)")
	}
	return t.UnixMicro(), nil
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}
