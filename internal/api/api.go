// Package api wires the scheduler to net/http handlers.
package api

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"time"

	"dagexec/internal/dag"
	"dagexec/internal/scheduler"
)

// Server holds the HTTP handlers for one scheduler instance.
type Server struct {
	sched *scheduler.Scheduler
	mux   *http.ServeMux
}

// NewServer registers all routes on a fresh ServeMux.
func NewServer(sched *scheduler.Scheduler) *Server {
	s := &Server{sched: sched, mux: http.NewServeMux()}
	s.mux.HandleFunc("GET /health", s.handleHealth)
	s.mux.HandleFunc("GET /tasks", s.handleListTasks)
	s.mux.HandleFunc("POST /dags", s.handleSubmit)
	s.mux.HandleFunc("GET /dags", s.handleListDAGs)
	s.mux.HandleFunc("GET /dags/{id}", s.handleGetDAG)
	s.mux.HandleFunc("POST /dags/{id}/cancel", s.handleCancel)
	s.mux.HandleFunc("POST /dags/{id}/retry", s.handleRetry)
	s.mux.HandleFunc("GET /dags/{id}/wait", s.handleWait)
	return s
}

// Handler exposes the mux.
func (s *Server) Handler() http.Handler { return s.mux }

func writeJSON(w http.ResponseWriter, status int, v interface{}) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}

func (s *Server) handleHealth(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

// taskInfo describes one whitelisted task.
type taskInfo struct {
	Name        string `json:"name"`
	Description string `json:"description"`
}

var taskDescriptions = []taskInfo{
	{"noop", "Takes no inputs, returns null."},
	{"identity", "Returns params.value."},
	{"add", "Sum of params.values plus every upstream numeric result (integer if integral)."},
	{"mul", "Product of params.values plus every upstream numeric result."},
	{"concat", "Concatenates params.values and upstream results as strings; params.sep joins them."},
	{"collect", "Returns {\"params\": ..., \"upstream\": {depId: value}}."},
	{"fail", "Always fails with params.message (default text)."},
	{"flaky", "Fails for the first params.fail_times LIFETIME attempts, then returns params.succeed_with (default \"ok\"). Attempts are persisted, so the failure schedule survives restarts."},
	{"sleep", "Sleeps params.ms milliseconds or until cancelled; cancellation demo helper."},
}

func (s *Server) handleListTasks(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"tasks":         taskDescriptions,
		"dag_statuses":  []string{dag.DAGPending, dag.DAGRunning, dag.DAGSucceeded, dag.DAGFailed, dag.DAGCancelling, dag.DAGCancelled},
		"node_statuses": []string{dag.StatusPending, dag.StatusRunning, dag.StatusSuccess, dag.StatusFailed, dag.StatusBlocked, dag.StatusCancelled},
	})
}

func (s *Server) handleSubmit(w http.ResponseWriter, r *http.Request) {
	var spec dag.Spec
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(&spec); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
		return
	}
	st, err := s.sched.Submit(spec)
	if err != nil {
		var ve *dag.ValidationError
		if errors.As(err, &ve) {
			writeJSON(w, http.StatusBadRequest, map[string]interface{}{"error": ve.Error(), "details": ve.Errors})
			return
		}
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusCreated, st)
}

func (s *Server) handleListDAGs(w http.ResponseWriter, _ *http.Request) {
	list, err := s.sched.List()
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]interface{}{"dags": list})
}

func (s *Server) handleGetDAG(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	st, err := s.sched.Get(id)
	if errors.Is(err, scheduler.ErrNotFound) {
		writeErr(w, http.StatusNotFound, "dag not found: "+id)
		return
	}
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, st)
}

func (s *Server) handleCancel(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if err := s.sched.Cancel(id); err != nil {
		s.mapMutErr(w, id, err)
		return
	}
	st, _ := s.sched.Get(id)
	writeJSON(w, http.StatusAccepted, st)
}

func (s *Server) handleRetry(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if err := s.sched.Retry(id); err != nil {
		s.mapMutErr(w, id, err)
		return
	}
	st, _ := s.sched.Get(id)
	writeJSON(w, http.StatusAccepted, st)
}

// handleWait optionally blocks until the DAG becomes terminal: GET /dags/{id}/wait?timeout=5s
func (s *Server) handleWait(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	timeout := 30 * time.Second
	if raw := r.URL.Query().Get("timeout"); raw != "" {
		d, err := time.ParseDuration(raw)
		if err != nil || d <= 0 {
			writeErr(w, http.StatusBadRequest, "invalid timeout; use a Go duration, e.g. 5s or 1500ms")
			return
		}
		timeout = d
	}
	st, err := s.sched.Wait(r.Context(), id, timeout)
	if errors.Is(err, scheduler.ErrNotFound) {
		writeErr(w, http.StatusNotFound, "dag not found: "+id)
		return
	}
	if errors.Is(err, context.DeadlineExceeded) {
		writeJSON(w, http.StatusAccepted, st)
		return
	}
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, st)
}

func (s *Server) mapMutErr(w http.ResponseWriter, id string, err error) {
	switch {
	case errors.Is(err, scheduler.ErrNotFound):
		writeErr(w, http.StatusNotFound, "dag not found: "+id)
	case errors.Is(err, scheduler.ErrConflict):
		writeErr(w, http.StatusConflict, err.Error())
	default:
		writeErr(w, http.StatusInternalServerError, err.Error())
	}
}
