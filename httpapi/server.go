// Package httpapi exposes the scheduler over a local JSON HTTP API.
package httpapi

import (
	"encoding/json"
	"errors"
	"net/http"
	"time"

	"deadline-admission/scheduler"
)

// Server is a thin JSON wrapper around a *scheduler.Scheduler.
type Server struct {
	sch *scheduler.Scheduler
	mux *http.ServeMux
}

func NewServer(sch *scheduler.Scheduler) *Server {
	s := &Server{sch: sch, mux: http.NewServeMux()}
	s.mux.HandleFunc("POST /jobs", s.handleSubmit)
	s.mux.HandleFunc("GET /jobs", s.handleList)
	s.mux.HandleFunc("GET /jobs/{id}", s.handleGet)
	s.mux.HandleFunc("POST /jobs/{id}/cancel", s.handleCancel)
	s.mux.HandleFunc("GET /events", s.handleEvents)
	s.mux.HandleFunc("GET /stats", s.handleStats)
	return s
}

func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	s.mux.ServeHTTP(w, r)
}

type submitRequest struct {
	ID               string `json:"id"`
	ExecBoundMs      int64  `json:"exec_bound_ms"`
	DeadlineInMs     int64  `json:"deadline_in_ms"` // relative to submission time
	SimulateActualMs int64  `json:"simulate_actual_ms,omitempty"`
}

type jobView struct {
	ID          string `json:"id"`
	State       string `json:"state"`
	ExecBoundMs int64  `json:"exec_bound_ms"`
	Deadline    string `json:"deadline"`
	SubmittedAt string `json:"submitted_at"`
	StartedAt   string `json:"started_at,omitempty"`
	FinishedAt  string `json:"finished_at,omitempty"`
	DeadlineMet *bool  `json:"deadline_met,omitempty"`
}

func view(j *scheduler.Job) jobView {
	v := jobView{
		ID:          j.ID,
		State:       string(j.State),
		ExecBoundMs: j.ExecBound.Milliseconds(),
		Deadline:    j.Deadline.Format(time.RFC3339Nano),
		SubmittedAt: j.SubmittedAt.Format(time.RFC3339Nano),
	}
	if !j.StartedAt.IsZero() {
		v.StartedAt = j.StartedAt.Format(time.RFC3339Nano)
	}
	if !j.FinishedAt.IsZero() {
		v.FinishedAt = j.FinishedAt.Format(time.RFC3339Nano)
	}
	if met, ok := j.DeadlineMet(); ok {
		v.DeadlineMet = &met
	}
	return v
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, code int, msg string) {
	writeJSON(w, code, map[string]string{"error": msg})
}

func (s *Server) handleSubmit(w http.ResponseWriter, r *http.Request) {
	var req submitRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid JSON body")
		return
	}
	now := s.sch.Now()
	j, err := s.sch.Submit(scheduler.JobSpec{
		ID:        req.ID,
		ExecBound: time.Duration(req.ExecBoundMs) * time.Millisecond,
		Deadline:  now.Add(time.Duration(req.DeadlineInMs) * time.Millisecond),
		SimActual: time.Duration(req.SimulateActualMs) * time.Millisecond,
	})
	switch {
	case errors.Is(err, scheduler.ErrInfeasible):
		writeJSON(w, http.StatusUnprocessableEntity, map[string]any{
			"error": "infeasible",
			"job":   view(j),
		})
	case errors.Is(err, scheduler.ErrInvalidSpec):
		writeErr(w, http.StatusBadRequest, err.Error())
	case err != nil:
		writeErr(w, http.StatusInternalServerError, err.Error())
	default:
		writeJSON(w, http.StatusCreated, map[string]any{"job": view(j)})
	}
}

func (s *Server) handleList(w http.ResponseWriter, r *http.Request) {
	jobs := s.sch.List()
	views := make([]jobView, 0, len(jobs))
	for _, j := range jobs {
		views = append(views, view(j))
	}
	writeJSON(w, http.StatusOK, map[string]any{"jobs": views})
}

func (s *Server) handleGet(w http.ResponseWriter, r *http.Request) {
	j, ok := s.sch.Get(r.PathValue("id"))
	if !ok {
		writeErr(w, http.StatusNotFound, "job not found")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"job": view(j)})
}

func (s *Server) handleCancel(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	err := s.sch.Cancel(id)
	switch {
	case errors.Is(err, scheduler.ErrNotFound):
		writeErr(w, http.StatusNotFound, "job not found")
	case errors.Is(err, scheduler.ErrNotCancellable):
		writeErr(w, http.StatusConflict, "job is not in a cancellable state")
	case err != nil:
		writeErr(w, http.StatusInternalServerError, err.Error())
	default:
		j, _ := s.sch.Get(id)
		writeJSON(w, http.StatusOK, map[string]any{"job": view(j)})
	}
}

func (s *Server) handleEvents(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"events": s.sch.Log().Events()})
}

func (s *Server) handleStats(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, s.sch.Stats())
}
