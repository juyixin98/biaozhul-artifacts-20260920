// Package httpserver exposes the scheduler over a small local HTTP API.
//
// Endpoints (JSON in/out, times are integer Unix milliseconds):
//
//	POST   /jobs                 submit (admission may reject with 422)
//	GET    /jobs                 list all jobs
//	GET    /jobs/{id}            one job
//	POST   /jobs/{id}/cancel     cancel a queued/running job
//	GET    /stats                counters + resource-conservation invariant
//	GET    /events               structured state-change log
//	GET    /healthz              liveness
package httpserver

import (
	"encoding/json"
	"errors"
	"net/http"
	"strings"
	"time"

	"deadlineadm"
	"deadlineadm/event"
)

// Server wraps a scheduler with HTTP handlers.
type Server struct {
	Sched *deadlineadm.Scheduler
	now   func() time.Time
}

// New builds a handler set over the scheduler.
func New(s *deadlineadm.Scheduler) *Server {
	return &Server{Sched: s, now: s.Now}
}

// Handler returns a configured *http.ServeMux.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /jobs", s.submit)
	mux.HandleFunc("GET /jobs", s.list)
	mux.HandleFunc("GET /jobs/{id}", s.get)
	mux.HandleFunc("POST /jobs/{id}/cancel", s.cancel)
	mux.HandleFunc("GET /stats", s.stats)
	mux.HandleFunc("GET /events", s.events)
	mux.HandleFunc("GET /healthz", s.health)
	return mux
}

// submitRequest is the JSON body of POST /jobs. All durations are
// milliseconds; deadline_ms is an absolute Unix-millisecond instant unless
// deadline_rel_ms is given, in which case it is relative to server time.
type submitRequest struct {
	ID            string `json:"id"`
	Payload       string `json:"payload"`
	Demand        int    `json:"demand"`
	DeadlineMs    int64  `json:"deadline_ms"`
	DeadlineRelMs int64  `json:"deadline_rel_ms"`
	BudgetMs      int64  `json:"budget_ms"`
}

type submitResponse struct {
	Status string               `json:"status"`
	Job    *deadlineadm.JobView `json:"job,omitempty"`
	Error  string               `json:"error,omitempty"`
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func (s *Server) submit(w http.ResponseWriter, r *http.Request) {
	var in submitRequest
	if err := json.NewDecoder(r.Body).Decode(&in); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid JSON: " + err.Error()})
		return
	}
	if in.BudgetMs <= 0 {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "budget_ms must be positive"})
		return
	}
	if in.DeadlineMs == 0 && in.DeadlineRelMs <= 0 {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "deadline_ms or deadline_rel_ms is required"})
		return
	}

	now := s.now()
	deadline := time.UnixMilli(in.DeadlineMs)
	if in.DeadlineRelMs > 0 {
		deadline = now.Add(time.Duration(in.DeadlineRelMs) * time.Millisecond)
	}
	demand := in.Demand
	if demand == 0 {
		demand = 1
	}

	err := s.Sched.Submit(deadlineadm.Request{
		ID:       in.ID,
		Payload:  in.Payload,
		Demand:   demand,
		Deadline: deadline,
		Budget:   time.Duration(in.BudgetMs) * time.Millisecond,
	})
	if err != nil {
		if errors.Is(err, deadlineadm.ErrInfeasible) {
			writeJSON(w, http.StatusUnprocessableEntity, submitResponse{Status: "rejected", Error: err.Error()})
			return
		}
		writeJSON(w, http.StatusBadRequest, submitResponse{Status: "rejected", Error: err.Error()})
		return
	}
	v, _ := s.Sched.Get(in.ID)
	writeJSON(w, http.StatusAccepted, submitResponse{Status: "admitted", Job: &v})
}

func (s *Server) list(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"jobs": s.Sched.List()})
}

func (s *Server) get(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	v, ok := s.Sched.Get(id)
	if !ok {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "job not found: " + id})
		return
	}
	writeJSON(w, http.StatusOK, v)
}

func (s *Server) cancel(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	err := s.Sched.Cancel(id)
	switch {
	case errors.Is(err, deadlineadm.ErrNotFound):
		writeJSON(w, http.StatusNotFound, map[string]string{"error": err.Error()})
	case errors.Is(err, deadlineadm.ErrTerminal):
		writeJSON(w, http.StatusConflict, map[string]string{"error": err.Error()})
	case err != nil:
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
	default:
		v, _ := s.Sched.Get(id)
		writeJSON(w, http.StatusOK, map[string]any{"status": "canceled", "job": v})
	}
}

func (s *Server) stats(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, s.Sched.Stats())
}

func (s *Server) events(w http.ResponseWriter, r *http.Request) {
	ev := s.Sched.Events()
	if ev == nil {
		ev = []event.Event{}
	}
	// Allow filtering by job id for readability: /events?job_id=j1.
	if jid := r.URL.Query().Get("job_id"); jid != "" {
		filtered := make([]event.Event, 0, len(ev))
		for _, e := range ev {
			if e.JobID == jid || (e.Type == event.Rejected && strings.Contains(e.Reason, jid)) {
				filtered = append(filtered, e)
			}
		}
		ev = filtered
	}
	writeJSON(w, http.StatusOK, map[string]any{"events": ev})
}

func (s *Server) health(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}
