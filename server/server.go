// Package server exposes a pool.Pool over a small local HTTP API.
package server

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"sync"
	"time"

	"dynpool/pool"
)

// Server owns one pool and routes HTTP requests to it.
type Server struct {
	mu  sync.RWMutex
	p   *pool.Pool
	rec *pool.EventRecorder
	mux *http.ServeMux
}

// Options configures a Server and its pool.
type Options struct {
	Name          string
	Workers       int
	QueueCapacity int
	Policy        pool.RejectPolicy
}

// New creates a server (and its pool).
func New(opts Options) (*Server, error) {
	if opts.Name == "" {
		opts.Name = "default"
	}
	rec := pool.NewEventRecorder(2048)
	p, err := pool.New(pool.Config{
		Name:          opts.Name,
		Workers:       opts.Workers,
		QueueCapacity: opts.QueueCapacity,
		Policy:        opts.Policy,
		Sink:          rec,
	})
	if err != nil {
		return nil, err
	}
	s := &Server{p: p, rec: rec}
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", s.handleHealth)
	mux.HandleFunc("/v1/pool", s.handlePool)
	mux.HandleFunc("/v1/pool/resize", s.handleResize)
	mux.HandleFunc("/v1/pool/shutdown", s.handleShutdown)
	mux.HandleFunc("/v1/tasks", s.handleTasks)
	mux.HandleFunc("/v1/tasks/", s.handleTask)
	mux.HandleFunc("/v1/events", s.handleEvents)
	s.mux = mux
	return s, nil
}

// Handler returns the HTTP handler.
func (s *Server) Handler() http.Handler { return s.mux }

// Pool returns the underlying pool.
func (s *Server) Pool() *pool.Pool { return s.p }

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

// GET /v1/pool -> stats
func (s *Server) handlePool(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	writeJSON(w, http.StatusOK, s.p.Stats())
}

// POST /v1/pool/resize  body {"workers": n}
func (s *Server) handleResize(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	var body struct {
		Workers int `json:"workers"`
	}
	if err := decodeJSON(r, &body); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	if err := s.p.Resize(body.Workers); err != nil {
		status := http.StatusInternalServerError
		switch {
		case errors.Is(err, pool.ErrInvalidConfig):
			status = http.StatusBadRequest
		case errors.Is(err, pool.ErrPoolShuttingDown):
			status = http.StatusConflict
		}
		writeError(w, status, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, s.p.Stats())
}

// POST /v1/pool/shutdown            graceful (drains accepted tasks)
// POST /v1/pool/shutdown?force=1    cancels pending, interrupts ctx
// optional &timeout_ms=N bounds the wait.
func (s *Server) handleShutdown(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	force := r.URL.Query().Get("force") == "1" || r.URL.Query().Get("force") == "true"
	timeout := 10 * time.Second
	if ms := r.URL.Query().Get("timeout_ms"); ms != "" {
		if n, err := strconv.Atoi(ms); err == nil && n > 0 {
			timeout = time.Duration(n) * time.Millisecond
		}
	}
	ctx, cancel := context.WithTimeout(r.Context(), timeout)
	defer cancel()

	if force {
		notRun, err := s.p.ShutdownNow(ctx)
		resp := map[string]any{"stats": s.p.Stats(), "not_run": notRun}
		if err != nil {
			writeErrorJSON(w, http.StatusRequestTimeout, err.Error(), resp)
			return
		}
		writeJSON(w, http.StatusOK, resp)
		return
	}
	if err := s.p.Shutdown(ctx); err != nil {
		writeError(w, http.StatusRequestTimeout, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, s.p.Stats())
}

// GET  /v1/tasks  -> all task snapshots
// POST /v1/tasks  -> submit a built-in demo task
func (s *Server) handleTasks(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodGet:
		writeJSON(w, http.StatusOK, map[string]any{"tasks": s.p.Tasks()})
		return
	case http.MethodPost:
	default:
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	var body struct {
		ID      string `json:"id"`
		Type    string `json:"type"`
		SleepMS int64  `json:"sleep_ms"`
		Fail    bool   `json:"fail"`
	}
	if err := decodeJSON(r, &body); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	sleep := time.Duration(body.SleepMS) * time.Millisecond
	fut, err := s.p.Submit(makeDemoTask(body.ID, body.Type, sleep, body.Fail))
	if err != nil {
		status := http.StatusBadRequest
		switch {
		case errors.Is(err, pool.ErrQueueFull), errors.Is(err, pool.ErrTaskDiscarded):
			status = http.StatusServiceUnavailable
		case errors.Is(err, pool.ErrPoolShuttingDown):
			status = http.StatusConflict
		}
		writeError(w, status, err.Error())
		return
	}
	writeJSON(w, http.StatusAccepted, fut.Snapshot())
}

// GET /v1/tasks/{id}
func (s *Server) handleTask(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	id := r.URL.Path[len("/v1/tasks/"):]
	if id == "" {
		writeError(w, http.StatusBadRequest, "missing task id")
		return
	}
	snap, ok := s.p.Task(id)
	if !ok {
		writeError(w, http.StatusNotFound, "task not found: "+id)
		return
	}
	writeJSON(w, http.StatusOK, snap)
}

// GET /v1/events -> retained structured events
func (s *Server) handleEvents(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"events": s.rec.Events()})
}

// ------------------------------------------------------------------ helpers

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}

func writeErrorJSON(w http.ResponseWriter, status int, msg string, detail any) {
	writeJSON(w, status, map[string]any{"error": msg, "detail": detail})
}

func decodeJSON(r *http.Request, v any) error {
	dec := json.NewDecoder(http.MaxBytesReader(nil, r.Body, 1<<20))
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		return fmt.Errorf("invalid JSON body: %w", err)
	}
	return nil
}
