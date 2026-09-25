// Package api exposes the HTTP ingestion/query surface.
//
//	Routes
//	  POST   /api/traces                  ingest one trace
//	  GET    /api/traces                  list trace ids
//	  GET    /api/traces/{id}             fetch a trace
//	  DELETE /api/traces/{id}             delete a trace
//	  GET    /api/traces/{id}/critical    analyze: self times + critical path
//	  POST   /api/synthetic/seed?name=... persist a built-in demo trace
//	  GET    /healthz                     liveness
package api

import (
	"encoding/json"
	"errors"
	"net/http"
	"strings"

	"cpathtrace/internal/analyzer"
	"cpathtrace/internal/model"
	"cpathtrace/internal/store"
	"cpathtrace/internal/synthetic"
)

// Server wires the store to HTTP handlers.
type Server struct {
	Store *store.FileStore
	Mux   *http.ServeMux
}

// NewServer builds the router.
func NewServer(st *store.FileStore) *Server {
	s := &Server{Store: st, Mux: http.NewServeMux()}
	s.routes()
	return s
}

type envelope struct {
	Data  any      `json:"data,omitempty"`
	Error *errBody `json:"error,omitempty"`
}

type errBody struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, status int, code, msg string) {
	writeJSON(w, status, envelope{Error: &errBody{Code: code, Message: msg}})
}

func (s *Server) routes() {
	s.Mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	s.Mux.HandleFunc("/api/traces", s.handleTraces)
	s.Mux.HandleFunc("/api/traces/", s.handleTraceByID)
	s.Mux.HandleFunc("/api/synthetic/seed", s.handleSeed)
}

func (s *Server) handleTraces(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodPost:
		s.ingest(w, r)
	case http.MethodGet:
		ids, err := s.Store.List()
		if err != nil {
			writeErr(w, http.StatusInternalServerError, "store_error", err.Error())
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"trace_ids": ids})
	default:
		writeErr(w, http.StatusMethodNotAllowed, "method_not_allowed", r.Method)
	}
}

// validateTrace performs ingestion-time validation. Analysis-level
// problems (skew, overlap, missing parent, cycle) are intentionally NOT
// rejected: they are stored and surfaced by the analyzer.
func validateTrace(t *model.Trace) error {
	if strings.TrimSpace(t.TraceID) == "" {
		return errors.New("trace_id is required")
	}
	if t.TraceID != strings.TrimSpace(t.TraceID) {
		return errors.New("trace_id must not contain surrounding whitespace")
	}
	if len(t.Spans) == 0 {
		return errors.New("spans must not be empty")
	}
	seen := map[string]bool{}
	for i := range t.Spans {
		sp := &t.Spans[i]
		if strings.TrimSpace(sp.SpanID) == "" {
			return errors.New("each span requires span_id")
		}
		if strings.TrimSpace(sp.Name) == "" {
			return errors.New("span " + sp.SpanID + " requires name")
		}
		if seen[sp.SpanID] {
			return errors.New("duplicate span_id within trace: " + sp.SpanID)
		}
		seen[sp.SpanID] = true
		if err := sp.Normalize(); err != nil {
			return err
		}
	}
	return nil
}

func (s *Server) ingest(w http.ResponseWriter, r *http.Request) {
	var t model.Trace
	dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4<<20))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&t); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid_json", err.Error())
		return
	}
	if err := validateTrace(&t); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid_trace", err.Error())
		return
	}
	if err := s.Store.Save(t); err != nil {
		writeErr(w, http.StatusInternalServerError, "store_error", err.Error())
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{
		"trace_id":   t.TraceID,
		"span_count": len(t.Spans),
	})
}

func (s *Server) handleTraceByID(w http.ResponseWriter, r *http.Request) {
	rest := strings.TrimPrefix(r.URL.Path, "/api/traces/")
	parts := strings.Split(rest, "/")
	if len(parts) == 0 || parts[0] == "" {
		writeErr(w, http.StatusBadRequest, "bad_path", "missing trace id")
		return
	}
	id := parts[0]
	switch {
	case len(parts) == 1:
		s.traceByID(w, r, id)
	case len(parts) == 2 && parts[1] == "critical":
		s.critical(w, r, id)
	default:
		writeErr(w, http.StatusNotFound, "not_found", r.URL.Path)
	}
}

func (s *Server) traceByID(w http.ResponseWriter, r *http.Request, id string) {
	switch r.Method {
	case http.MethodGet:
		t, err := s.Store.Get(id)
		if errors.Is(err, store.ErrNotFound) {
			writeErr(w, http.StatusNotFound, "not_found", "trace "+id+" not found")
			return
		}
		if err != nil {
			writeErr(w, http.StatusInternalServerError, "store_error", err.Error())
			return
		}
		writeJSON(w, http.StatusOK, t)
	case http.MethodDelete:
		if err := s.Store.Delete(id); err != nil {
			if errors.Is(err, store.ErrNotFound) {
				writeErr(w, http.StatusNotFound, "not_found", "trace "+id+" not found")
				return
			}
			writeErr(w, http.StatusInternalServerError, "store_error", err.Error())
			return
		}
		writeJSON(w, http.StatusOK, map[string]string{"deleted": id})
	default:
		writeErr(w, http.StatusMethodNotAllowed, "method_not_allowed", r.Method)
	}
}

func (s *Server) critical(w http.ResponseWriter, r *http.Request, id string) {
	if r.Method != http.MethodGet {
		writeErr(w, http.StatusMethodNotAllowed, "method_not_allowed", r.Method)
		return
	}
	t, err := s.Store.Get(id)
	if errors.Is(err, store.ErrNotFound) {
		writeErr(w, http.StatusNotFound, "not_found", "trace "+id+" not found")
		return
	}
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "store_error", err.Error())
		return
	}
	res := analyzer.Analyze(t)
	status := http.StatusOK
	if res.HasError() {
		// Analysis succeeded but the data is not a valid DAG / has no
		// root: make that visible at the HTTP level too.
		status = http.StatusUnprocessableEntity
	}
	writeJSON(w, status, res)
}

func (s *Server) handleSeed(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeErr(w, http.StatusMethodNotAllowed, "method_not_allowed", r.Method)
		return
	}
	name := r.URL.Query().Get("name")
	if name == "" {
		writeErr(w, http.StatusBadRequest, "bad_name",
			"name query parameter required; one of: "+strings.Join(synthetic.Names(), ","))
		return
	}
	t, ok := synthetic.Build(name)
	if !ok {
		writeErr(w, http.StatusBadRequest, "bad_name",
			"unknown scenario "+name+"; one of: "+strings.Join(synthetic.Names(), ","))
		return
	}
	if err := s.Store.Save(t); err != nil {
		writeErr(w, http.StatusInternalServerError, "store_error", err.Error())
		return
	}
	res := analyzer.Analyze(t)
	writeJSON(w, http.StatusCreated, map[string]any{
		"trace_id": t.TraceID,
		"analysis": res,
	})
}
