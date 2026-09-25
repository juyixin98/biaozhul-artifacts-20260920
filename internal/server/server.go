// Package server exposes the scheduler engine over a small local HTTP API.
package server

import (
	"encoding/json"
	"log"
	"net/http"
	"time"

	"dagscheduler/internal/executor"
	"dagscheduler/scheduler"
)

// Server wires the engine, event log and HTTP routes.
type Server struct {
	engine *scheduler.Engine
	log    *scheduler.SliceSink
	mux    *http.ServeMux
}

// New builds a Server with the demo executor and a bounded event log.
func New(backoff time.Duration, eventLogCap int) *Server {
	eventLog := scheduler.NewBoundedSliceSink(eventLogCap)
	dem := executor.NewDemo()
	eng := scheduler.New(dem,
		scheduler.WithSink(eventLog),
		scheduler.WithBackoff(func(int) time.Duration { return backoff }),
	)
	s := &Server{engine: eng, log: eventLog, mux: http.NewServeMux()}
	s.routes()
	return s
}

// Handler exposes the HTTP mux (useful for tests and embedding).
func (s *Server) Handler() http.Handler { return s.mux }

func (s *Server) routes() {
	s.mux.HandleFunc("GET /healthz", s.handleHealth)
	s.mux.HandleFunc("POST /v1/jobs", s.handleSubmit)
	s.mux.HandleFunc("GET /v1/jobs", s.handleList)
	s.mux.HandleFunc("GET /v1/jobs/{id}", s.handleGet)
	s.mux.HandleFunc("POST /v1/jobs/{id}/cancel", s.handleCancel)
	s.mux.HandleFunc("GET /v1/jobs/{id}/events", s.handleEvents)
}

func (s *Server) handleHealth(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *Server) handleSubmit(w http.ResponseWriter, r *http.Request) {
	var spec scheduler.DagSpec
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(&spec); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
		return
	}
	job, err := s.engine.Submit(&spec)
	if err != nil {
		// Validation/cycle errors are client errors.
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{
		"id":     job.ID,
		"status": "accepted",
		"get":    "/v1/jobs/" + job.ID,
	})
}

func (s *Server) handleList(w http.ResponseWriter, _ *http.Request) {
	jobs := s.engine.List()
	snaps := make([]scheduler.JobSnapshot, 0, len(jobs))
	for _, j := range jobs {
		snaps = append(snaps, j.Snapshot())
	}
	writeJSON(w, http.StatusOK, map[string]any{"jobs": snaps})
}

func (s *Server) handleGet(w http.ResponseWriter, r *http.Request) {
	job, ok := s.engine.Get(r.PathValue("id"))
	if !ok {
		writeError(w, http.StatusNotFound, "job not found")
		return
	}
	writeJSON(w, http.StatusOK, job.Snapshot())
}

func (s *Server) handleCancel(w http.ResponseWriter, r *http.Request) {
	job, ok := s.engine.Get(r.PathValue("id"))
	if !ok {
		writeError(w, http.StatusNotFound, "job not found")
		return
	}
	if !job.Cancel() {
		writeError(w, http.StatusConflict, "job already finished")
		return
	}
	writeJSON(w, http.StatusAccepted, map[string]string{"status": "cancel requested"})
}

func (s *Server) handleEvents(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if _, ok := s.engine.Get(id); !ok {
		writeError(w, http.StatusNotFound, "job not found")
		return
	}
	events := s.log.EventsFor(id)
	writeJSON(w, http.StatusOK, map[string]any{"jobId": id, "events": events})
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(v); err != nil {
		log.Printf("write response: %v", err)
	}
}

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]any{"error": http.StatusText(status), "message": msg})
}
