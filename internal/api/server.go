// Package api exposes the admission service over HTTP using chi. Pure JSON
// backend: no HTML, no browser UI.
package api

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"

	"mirror-admission/internal/model"
	"mirror-admission/internal/service"
	"mirror-admission/internal/store"
)

// maxBody bounds request size (config+SBOM+envelopes). 16 MiB.
const maxBody = 16 << 20

type Server struct {
	svc *service.Service
}

func NewServer(svc *service.Service) http.Handler {
	s := &Server{svc: svc}
	r := chi.NewRouter()
	r.Use(middleware.RealIP)
	r.Use(middleware.RequestID)
	r.Use(middleware.Recoverer)
	r.Use(noCache)

	r.Get("/healthz", s.health)
	r.Get("/v1/meta", s.meta)
	r.Post("/v1/admission/evaluate", s.evaluate)
	r.Get("/v1/reports", s.listReports)
	r.Get("/v1/reports/{id}", s.getReport)
	return r
}

func noCache(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		next.ServeHTTP(w, r)
	})
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	_ = enc.Encode(v)
}

type errorBody struct {
	Error string `json:"error"`
}

func writeError(w http.ResponseWriter, status int, err error) {
	writeJSON(w, status, errorBody{Error: err.Error()})
}

func (s *Server) health(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *Server) meta(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, s.svc.Meta())
}

func (s *Server) evaluate(w http.ResponseWriter, r *http.Request) {
	body, err := io.ReadAll(io.LimitReader(r.Body, maxBody))
	if err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	if len(body) == int(maxBody) {
		writeError(w, http.StatusRequestEntityTooLarge, errors.New("request body exceeds 16 MiB"))
		return
	}
	var req model.AdmissionRequest
	if err := service.DecodeStrict(body, &req); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	report, err := s.svc.Evaluate(r.Context(), req)
	if err != nil {
		if errors.Is(err, service.ErrBadRequest) {
			writeError(w, http.StatusBadRequest, err)
			return
		}
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	// Decision semantics do NOT map ALLOW alone to 200: the resource exists
	// and evaluation completed, so 201 Created for the immutable report.
	w.Header().Set("Location", "/v1/reports/"+report.ID)
	writeJSON(w, http.StatusCreated, report)
}

func (s *Server) listReports(w http.ResponseWriter, r *http.Request) {
	reports, err := s.svc.ListReports(r.Context())
	if err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	if reports == nil {
		reports = []model.Report{}
	}
	writeJSON(w, http.StatusOK, map[string]any{"reports": reports})
}

func (s *Server) getReport(w http.ResponseWriter, r *http.Request) {
	id := chi.URLParam(r, "id")
	rep, err := s.svc.GetReport(r.Context(), id)
	if err != nil {
		if errors.Is(err, store.ErrNotFound) {
			writeError(w, http.StatusNotFound, err)
			return
		}
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	writeJSON(w, http.StatusOK, rep)
}
