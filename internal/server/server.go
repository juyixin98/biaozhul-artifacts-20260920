// Package server exposes the engine over a small JSON HTTP API using only
// net/http. No UI, no third-party dependencies.
package server

import (
	"encoding/json"
	"errors"
	"net/http"

	"dagexec/internal/engine"
	"dagexec/internal/model"
)

// Server wires HTTP handlers to an engine.
type Server struct {
	eng *engine.Engine
	mux *http.ServeMux
}

// New creates the HTTP handler.
func New(eng *engine.Engine) *Server {
	s := &Server{eng: eng, mux: http.NewServeMux()}
	s.routes()
	return s
}

func (s *Server) Handler() http.Handler { return s.mux }

func (s *Server) routes() {
	s.mux.HandleFunc("GET /healthz", s.health)
	s.mux.HandleFunc("GET /api/tasks", s.tasks)
	s.mux.HandleFunc("POST /api/dags", s.submit)
	s.mux.HandleFunc("GET /api/dags", s.list)
	s.mux.HandleFunc("GET /api/dags/{id}", s.get)
	s.mux.HandleFunc("POST /api/dags/{id}/cancel", s.cancel)
}

func (s *Server) health(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *Server) tasks(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string][]string{"tasks": s.eng.TaskNames()})
}

func (s *Server) submit(w http.ResponseWriter, r *http.Request) {
	var spec model.DAG
	dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&spec); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
		return
	}
	snap, err := s.eng.Submit(spec)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, http.StatusCreated, snap)
}

func (s *Server) list(w http.ResponseWriter, _ *http.Request) {
	snaps, err := s.eng.List()
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if snaps == nil {
		snaps = []*model.Snapshot{}
	}
	writeJSON(w, http.StatusOK, snaps)
}

func (s *Server) get(w http.ResponseWriter, r *http.Request) {
	snap, err := s.eng.Get(r.PathValue("id"))
	if err != nil {
		if errors.Is(err, engine.ErrNotFound) {
			writeError(w, http.StatusNotFound, "dag not found")
			return
		}
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, snap)
}

func (s *Server) cancel(w http.ResponseWriter, r *http.Request) {
	snap, err := s.eng.Cancel(r.PathValue("id"))
	if err != nil {
		if errors.Is(err, engine.ErrNotFound) {
			writeError(w, http.StatusNotFound, "dag not found")
			return
		}
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, snap)
}

type errorBody struct {
	Error string `json:"error"`
}

func writeError(w http.ResponseWriter, code int, msg string) {
	writeJSON(w, code, errorBody{Error: msg})
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(code)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}
