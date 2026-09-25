package api

import (
	"encoding/json"
	"log"
	"net/http"
	"strconv"
	"strings"

	"tailsampler/internal/sampler"
)

// Server exposes the sampler over HTTP.
type Server struct {
	s   *sampler.Sampler
	mux *http.ServeMux
}

// NewServer wires all routes.
func NewServer(s *sampler.Sampler) *Server {
	srv := &Server{s: s, mux: http.NewServeMux()}
	srv.mux.HandleFunc("POST /v1/spans", srv.handleIngest)
	srv.mux.HandleFunc("GET /v1/decisions", srv.handleListDecisions)
	srv.mux.HandleFunc("GET /v1/decisions/", srv.handleGetDecision)
	srv.mux.HandleFunc("GET /v1/traces/", srv.handleGetTrace)
	srv.mux.HandleFunc("GET /v1/stats", srv.handleStats)
	srv.mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	return srv
}

func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) { s.mux.ServeHTTP(w, r) }

type ingestRequest struct {
	Spans []sampler.Span `json:"spans"`
}

type ingestResponse struct {
	Accepted int                             `json:"accepted"`
	Traces   map[string]sampler.IngestStatus `json:"traces"`
}

func (s *Server) handleIngest(w http.ResponseWriter, r *http.Request) {
	var req ingestRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
		return
	}
	if len(req.Spans) == 0 {
		writeError(w, http.StatusBadRequest, "spans must not be empty")
		return
	}
	for i, sp := range req.Spans {
		if sp.TraceID == "" || sp.SpanID == "" {
			writeError(w, http.StatusBadRequest, "spans["+strconv.Itoa(i)+"]: trace_id and span_id are required")
			return
		}
	}
	statuses := s.s.Ingest(req.Spans)
	writeJSON(w, http.StatusAccepted, ingestResponse{Accepted: len(req.Spans), Traces: statuses})
}

func (s *Server) handleGetDecision(w http.ResponseWriter, r *http.Request) {
	id := strings.TrimPrefix(r.URL.Path, "/v1/decisions/")
	d, ok := s.s.GetDecision(id)
	if !ok {
		writeError(w, http.StatusNotFound, "no decision for trace "+id)
		return
	}
	writeJSON(w, http.StatusOK, d)
}

func (s *Server) handleListDecisions(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	keep := parseBoolPtr(q.Get("keep"))
	degraded := parseBoolPtr(q.Get("degraded"))
	incomplete := parseBoolPtr(q.Get("incomplete"))
	limit, _ := strconv.Atoi(q.Get("limit"))
	writeJSON(w, http.StatusOK, map[string]any{
		"decisions": s.s.ListDecisions(keep, degraded, incomplete, limit),
	})
}

func (s *Server) handleGetTrace(w http.ResponseWriter, r *http.Request) {
	id := strings.TrimPrefix(r.URL.Path, "/v1/traces/")
	spans, ok := s.s.GetTrace(id)
	if !ok {
		writeError(w, http.StatusNotFound, "trace "+id+" was not kept (or unknown)")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"trace_id": id, "spans": spans})
}

func (s *Server) handleStats(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, s.s.Stats())
}

func parseBoolPtr(v string) *bool {
	if v == "" {
		return nil
	}
	b, err := strconv.ParseBool(v)
	if err != nil {
		return nil
	}
	return &b
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	if err := json.NewEncoder(w).Encode(v); err != nil {
		log.Printf("write response: %v", err)
	}
}

func writeError(w http.ResponseWriter, code int, msg string) {
	writeJSON(w, code, map[string]string{"error": msg})
}
