// Package server exposes the scheduler engine over a small local HTTP API.
package server

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"

	"dagscheduler/scheduler"
)

// Server wires an *scheduler.Engine to HTTP handlers.
type Server struct {
	engine *scheduler.Engine
	mux    *http.ServeMux
}

// New builds a server around an existing engine.
func New(eng *scheduler.Engine) *Server {
	s := &Server{engine: eng, mux: http.NewServeMux()}
	s.routes()
	return s
}

// Handler returns the root http.Handler (useful for tests and custom servers).
func (s *Server) Handler() http.Handler { return s.mux }

func (s *Server) routes() {
	s.mux.HandleFunc("GET /health", s.handleHealth)
	s.mux.HandleFunc("POST /runs", s.handleCreateRun)
	s.mux.HandleFunc("GET /runs", s.handleListRuns)
	s.mux.HandleFunc("GET /runs/{id}", s.handleGetRun)
	s.mux.HandleFunc("GET /runs/{id}/events", s.handleGetEvents)
	s.mux.HandleFunc("POST /runs/{id}/cancel", s.handleCancelRun)
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *Server) handleCreateRun(w http.ResponseWriter, r *http.Request) {
	var dag scheduler.DAG
	if err := decodeJSON(r, &dag); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	snap, err := s.engine.Submit(dag)
	if err != nil {
		// Validation failures (cycles, unknown deps, bad policy, unknown
		// task type) are client errors.
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, http.StatusCreated, snap)
}

func (s *Server) handleListRuns(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, s.engine.List())
}

func (s *Server) handleGetRun(w http.ResponseWriter, r *http.Request) {
	snap, err := s.engine.Get(r.PathValue("id"))
	if err != nil {
		writeEngineError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, snap)
}

func (s *Server) handleGetEvents(w http.ResponseWriter, r *http.Request) {
	events, err := s.engine.Events(r.PathValue("id"))
	if err != nil {
		writeEngineError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"events": events})
}

func (s *Server) handleCancelRun(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Reason string `json:"reason"`
	}
	// Body is optional; malformed (but non-empty) JSON is a client error.
	if r.Body != nil {
		if err := decodeJSONAllowEmpty(r, &body); err != nil {
			writeError(w, http.StatusBadRequest, err.Error())
			return
		}
	}
	snap, err := s.engine.Cancel(r.PathValue("id"), body.Reason)
	if err != nil {
		writeEngineError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, snap)
}

func writeEngineError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, scheduler.ErrNotFound):
		writeError(w, http.StatusNotFound, err.Error())
	case errors.Is(err, scheduler.ErrRunFinished):
		writeError(w, http.StatusConflict, err.Error())
	default:
		writeError(w, http.StatusInternalServerError, err.Error())
	}
}

func decodeJSON(r *http.Request, v any) error {
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	return dec.Decode(v)
}

func decodeJSONAllowEmpty(r *http.Request, v any) error {
	dec := json.NewDecoder(r.Body)
	if err := dec.Decode(v); err != nil && !errors.Is(err, io.EOF) {
		return err
	}
	return nil
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}
