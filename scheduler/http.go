package scheduler

import (
	"encoding/json"
	"net/http"
	"strconv"
	"strings"
)

// Server exposes the scheduler over a small JSON HTTP API.
type Server struct {
	sched *Scheduler
	mux   *http.ServeMux
}

// NewServer wires HTTP routes around an existing scheduler.
func NewServer(s *Scheduler) *Server {
	srv := &Server{sched: s, mux: http.NewServeMux()}
	srv.mux.HandleFunc("/healthz", srv.handleHealth)
	srv.mux.HandleFunc("/tenants", srv.handleTenants)
	srv.mux.HandleFunc("/tenants/", srv.handleTenant)
	srv.mux.HandleFunc("/tasks", srv.handleTasks)
	srv.mux.HandleFunc("/tasks/", srv.handleTask)
	srv.mux.HandleFunc("/snapshot", srv.handleSnapshot)
	srv.mux.HandleFunc("/events", srv.handleEvents)
	return srv
}

// Handler returns the http.Handler for mounting in a custom server.
func (s *Server) Handler() http.Handler { return s.mux }

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func errorJSON(w http.ResponseWriter, status int, err error) {
	writeJSON(w, status, map[string]string{"error": err.Error()})
}

// statusFor maps sentinel package errors to HTTP status codes.
func statusFor(err error) int {
	switch {
	case strings.Contains(err.Error(), ErrBadRequest.Error()):
		return http.StatusBadRequest
	case strings.Contains(err.Error(), ErrTenantNotFound.Error()),
		strings.Contains(err.Error(), ErrTaskNotFound.Error()):
		return http.StatusNotFound
	case strings.Contains(err.Error(), ErrTenantExists.Error()),
		strings.Contains(err.Error(), ErrTaskExists.Error()):
		return http.StatusConflict
	default:
		return http.StatusInternalServerError
	}
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		errorJSON(w, http.StatusMethodNotAllowed, errMethodNotAllowed)
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

type tenantRequest struct {
	ID     string `json:"id"`
	Weight int64  `json:"weight"`
}

// POST /tenants creates a tenant; GET /tenants lists tenants (via snapshot).
func (s *Server) handleTenants(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodPost:
		var req tenantRequest
		if err := decodeBody(r, &req); err != nil {
			errorJSON(w, http.StatusBadRequest, err)
			return
		}
		if err := s.sched.AddTenant(req.ID, req.Weight); err != nil {
			errorJSON(w, statusFor(err), err)
			return
		}
		writeJSON(w, http.StatusCreated, map[string]string{"id": req.ID, "status": "created"})
	case http.MethodGet:
		writeJSON(w, http.StatusOK, s.sched.Snapshot().Tenants)
	default:
		errorJSON(w, http.StatusMethodNotAllowed, errMethodNotAllowed)
	}
}

// GET /tenants/{id}
func (s *Server) handleTenant(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		errorJSON(w, http.StatusMethodNotAllowed, errMethodNotAllowed)
		return
	}
	id := strings.TrimPrefix(r.URL.Path, "/tenants/")
	for _, t := range s.sched.Snapshot().Tenants {
		if t.ID == id {
			writeJSON(w, http.StatusOK, t)
			return
		}
	}
	errorJSON(w, http.StatusNotFound, notFoundErr("tenant", id))
}

// POST /tasks submits a task; GET /tasks lists tasks (via snapshot).
func (s *Server) handleTasks(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodPost:
		var spec TaskSpec
		if err := decodeBody(r, &spec); err != nil {
			errorJSON(w, http.StatusBadRequest, err)
			return
		}
		if err := s.sched.Submit(spec); err != nil {
			errorJSON(w, statusFor(err), err)
			return
		}
		// Report the post-submit state of the task.
		snap := s.sched.Snapshot()
		for _, tk := range snap.Tasks {
			if tk.ID == spec.ID {
				writeJSON(w, http.StatusAccepted, tk)
				return
			}
		}
		writeJSON(w, http.StatusAccepted, map[string]string{"id": spec.ID, "status": "accepted"})
	case http.MethodGet:
		writeJSON(w, http.StatusOK, s.sched.Snapshot().Tasks)
	default:
		errorJSON(w, http.StatusMethodNotAllowed, errMethodNotAllowed)
	}
}

// GET /tasks/{id}
func (s *Server) handleTask(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		errorJSON(w, http.StatusMethodNotAllowed, errMethodNotAllowed)
		return
	}
	id := strings.TrimPrefix(r.URL.Path, "/tasks/")
	for _, tk := range s.sched.Snapshot().Tasks {
		if tk.ID == id {
			writeJSON(w, http.StatusOK, tk)
			return
		}
	}
	errorJSON(w, http.StatusNotFound, notFoundErr("task", id))
}

func (s *Server) handleSnapshot(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		errorJSON(w, http.StatusMethodNotAllowed, errMethodNotAllowed)
		return
	}
	writeJSON(w, http.StatusOK, s.sched.Snapshot())
}

// GET /events?after=<id>&limit=<n>
func (s *Server) handleEvents(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		errorJSON(w, http.StatusMethodNotAllowed, errMethodNotAllowed)
		return
	}
	var after int64
	if v := r.URL.Query().Get("after"); v != "" {
		n, err := strconv.ParseInt(v, 10, 64)
		if err != nil {
			errorJSON(w, http.StatusBadRequest, badRequestErr("after must be an integer"))
			return
		}
		after = n
	}
	var limit int
	if v := r.URL.Query().Get("limit"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n < 0 {
			errorJSON(w, http.StatusBadRequest, badRequestErr("limit must be a non-negative integer"))
			return
		}
		limit = n
	}
	writeJSON(w, http.StatusOK, s.sched.Events(after, limit))
}
