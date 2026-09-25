// Package api exposes the dependency scan service over a local JSON HTTP API.
package api

import (
	"encoding/json"
	"errors"
	"log"
	"net/http"

	"depscan/internal/scanner"
	"depscan/internal/service"
)

// Server is the JSON API handler.
type Server struct {
	svc *service.Service
	mux *http.ServeMux
}

// NewServer builds a Server on top of svc.
func NewServer(svc *service.Service) *Server {
	s := &Server{svc: svc, mux: http.NewServeMux()}
	s.mux.HandleFunc("GET /healthz", s.handleHealth)
	s.mux.HandleFunc("POST /v1/scan", s.handleFullScan)
	s.mux.HandleFunc("POST /v1/scan/incremental", s.handleIncremental)
	s.mux.HandleFunc("GET /v1/graph", s.handleGraph)
	return s
}

// Handler returns the root http.Handler.
func (s *Server) Handler() http.Handler { return s.mux }

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

func writeError(w http.ResponseWriter, err error) {
	status := http.StatusInternalServerError
	var cfgErr *service.ConfigError
	switch {
	case errors.Is(err, scanner.ErrMacroInclude):
		status = http.StatusUnprocessableEntity
	case errors.Is(err, service.ErrNoCache):
		status = http.StatusConflict
	case errors.As(err, &cfgErr):
		status = http.StatusBadRequest
	}
	log.Printf("request error: %v", err)
	writeJSON(w, status, map[string]string{"error": err.Error()})
}

func (s *Server) handleHealth(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

type scanRequest struct {
	service.Config
}

func (s *Server) handleFullScan(w http.ResponseWriter, r *http.Request) {
	var req scanRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid JSON: " + err.Error()})
		return
	}
	res, err := s.svc.FullScan(req.Config)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, res)
}

type incrementalRequest struct {
	service.Config
	Changed []string `json:"changed"` // files modified or added (root-relative)
	Deleted []string `json:"deleted"` // files removed (root-relative)
}

func (s *Server) handleIncremental(w http.ResponseWriter, r *http.Request) {
	var req incrementalRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid JSON: " + err.Error()})
		return
	}
	res, err := s.svc.Incremental(req.Config, req.Changed, req.Deleted)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, res)
}

func (s *Server) handleGraph(w http.ResponseWriter, r *http.Request) {
	root := r.URL.Query().Get("sourceRoot")
	if root == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "sourceRoot query parameter is required"})
		return
	}
	g, err := s.svc.LoadCache(root)
	if err != nil {
		writeError(w, err)
		return
	}
	if g == nil {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "no cached scan for this source root"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"graph": g.Deps})
}
