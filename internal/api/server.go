// Package api wires the DRF scheduler to a net/http handler exposing a JSON
// REST API. It uses only the Go standard library.
package api

import (
	"encoding/json"
	"errors"
	"net/http"
	"strings"

	"drf-scheduler/internal/scheduler"
)

// Server holds the dependencies for the HTTP handlers.
type Server struct {
	Sched *scheduler.Scheduler
}

// NewServer builds a Server around an existing scheduler.
func NewServer(s *scheduler.Scheduler) *Server { return &Server{Sched: s} }

// Mux registers all routes. Paths with an id are parsed manually so the code
// works on Go 1.21+ (no 1.22 method-pattern dependency).
func (s *Server) Mux() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/health", s.handleHealth)
	mux.HandleFunc("/config", s.handleConfig)
	mux.HandleFunc("/reset", s.handleReset)
	mux.HandleFunc("/tenants", s.handleTenants)
	mux.HandleFunc("/tenants/", s.handleTenantByID)
	mux.HandleFunc("/tasks", s.handleTasks)
	mux.HandleFunc("/tasks/", s.handleTaskByID)
	mux.HandleFunc("/schedule", s.handleSchedule)
	mux.HandleFunc("/state", s.handleState)
	return logging(mux)
}

// ---------- request/response types ----------

type configRequest struct {
	CPU int64 `json:"cpu"`
	Mem int64 `json:"mem"`
}

type tenantRequest struct {
	Name   string              `json:"name"`
	Weight int64               `json:"weight"`
	Quota  scheduler.Resources `json:"quota"`
}

type taskRequest struct {
	ID     string              `json:"id"`
	Tenant string              `json:"tenant"`
	Demand scheduler.Resources `json:"demand"`
}

type errBody struct {
	Error string `json:"error"`
}

// ---------- helpers ----------

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, errBody{Error: msg})
}

func decode(w http.ResponseWriter, r *http.Request, v any) bool {
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
		return false
	}
	return true
}

// mapError translates scheduler errors onto HTTP status codes.
func mapError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, scheduler.ErrBadRequest):
		writeErr(w, http.StatusBadRequest, err.Error())
	case errors.Is(err, scheduler.ErrNotConfigured):
		writeErr(w, http.StatusConflict, err.Error())
	case errors.Is(err, scheduler.ErrAlreadyConfig):
		writeErr(w, http.StatusConflict, err.Error())
	case errors.Is(err, scheduler.ErrInvalidCapacity):
		writeErr(w, http.StatusBadRequest, err.Error())
	case errors.Is(err, scheduler.ErrTenantExists), errors.Is(err, scheduler.ErrTaskExists):
		writeErr(w, http.StatusConflict, err.Error())
	case errors.Is(err, scheduler.ErrTenantNotFound), errors.Is(err, scheduler.ErrTaskNotFound):
		writeErr(w, http.StatusNotFound, err.Error())
	default:
		writeErr(w, http.StatusInternalServerError, err.Error())
	}
}

func methodNotAllowed(w http.ResponseWriter, allowed string) {
	w.Header().Set("Allow", allowed)
	writeErr(w, http.StatusMethodNotAllowed, "method not allowed; use "+allowed)
}

// logging is minimal request logging to the default logger.
func logging(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		next.ServeHTTP(w, r)
	})
}

// ---------- handlers ----------

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		methodNotAllowed(w, http.MethodGet)
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *Server) handleConfig(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodGet:
		writeJSON(w, http.StatusOK, s.Sched.Snapshot())
	case http.MethodPost:
		var req configRequest
		if !decode(w, r, &req) {
			return
		}
		if err := s.Sched.Configure(scheduler.Resources{CPU: req.CPU, Mem: req.Mem}); err != nil {
			mapError(w, err)
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"status": "configured", "capacity": req})
	default:
		methodNotAllowed(w, "GET, POST")
	}
}

func (s *Server) handleReset(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		methodNotAllowed(w, http.MethodPost)
		return
	}
	s.Sched.Reset()
	writeJSON(w, http.StatusOK, map[string]string{"status": "reset"})
}

func (s *Server) handleTenants(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		methodNotAllowed(w, http.MethodPost)
		return
	}
	var req tenantRequest
	if !decode(w, r, &req) {
		return
	}
	if req.Weight == 0 {
		req.Weight = 1 // default weight
	}
	if err := s.Sched.AddTenant(req.Name, req.Weight, req.Quota); err != nil {
		mapError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{
		"name": req.Name, "weight": req.Weight, "quota": req.Quota, "status": "created",
	})
}

// handleTenantByID serves GET /tenants/{name}.
func (s *Server) handleTenantByID(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		methodNotAllowed(w, http.MethodGet)
		return
	}
	name := strings.TrimPrefix(r.URL.Path, "/tenants/")
	if name == "" || strings.Contains(name, "/") {
		writeErr(w, http.StatusNotFound, "unknown path")
		return
	}
	snap := s.Sched.Snapshot()
	for _, t := range snap.Tenants {
		if t.Name == name {
			writeJSON(w, http.StatusOK, t)
			return
		}
	}
	writeErr(w, http.StatusNotFound, "tenant not found: "+name)
}

func (s *Server) handleTasks(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		methodNotAllowed(w, http.MethodPost)
		return
	}
	var req taskRequest
	if !decode(w, r, &req) {
		return
	}
	task, err := s.Sched.Submit(req.ID, req.Tenant, req.Demand)
	if err != nil {
		mapError(w, err)
		return
	}
	snap := s.Sched.Snapshot()
	state := "queued"
	for _, t := range snap.Running {
		if t.ID == task.ID {
			state = "running"
			break
		}
	}
	writeJSON(w, http.StatusAccepted, map[string]any{
		"id": task.ID, "tenant": task.Tenant, "demand": task.Demand,
		"seq": task.Seq, "state": state,
	})
}

// handleTaskByID serves GET /tasks/{id} and DELETE /tasks/{id} (release).
func (s *Server) handleTaskByID(w http.ResponseWriter, r *http.Request) {
	id := strings.TrimPrefix(r.URL.Path, "/tasks/")
	if id == "" || strings.Contains(id, "/") {
		writeErr(w, http.StatusNotFound, "unknown path")
		return
	}
	switch r.Method {
	case http.MethodGet:
		snap := s.Sched.Snapshot()
		for _, t := range snap.Running {
			if t.ID == id {
				writeJSON(w, http.StatusOK, t)
				return
			}
		}
		for _, t := range snap.Queued {
			if t.ID == id {
				writeJSON(w, http.StatusOK, t)
				return
			}
		}
		writeErr(w, http.StatusNotFound, "task not found: "+id)
	case http.MethodDelete:
		if err := s.Sched.Release(id); err != nil {
			mapError(w, err)
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"id": id, "status": "released", "state": s.Sched.Snapshot()})
	default:
		methodNotAllowed(w, "GET, DELETE")
	}
}

func (s *Server) handleSchedule(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		methodNotAllowed(w, http.MethodPost)
		return
	}
	s.Sched.Schedule()
	writeJSON(w, http.StatusOK, s.Sched.Snapshot())
}

func (s *Server) handleState(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		methodNotAllowed(w, http.MethodGet)
		return
	}
	writeJSON(w, http.StatusOK, s.Sched.Snapshot())
}
