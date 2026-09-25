// Package server exposes a work-stealing executor over a small local
// HTTP API. It only depends on the scheduler library and the standard
// library.
package server

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"strings"
	"time"

	"worksteal/scheduler"
)

// Server wraps an executor and its event ring buffer.
type Server struct {
	Exec    *scheduler.Executor
	events  *scheduler.MemorySink
	http    *http.Server
	baseURL string // set by tests using httptest
}

// Config configures New.
type Config struct {
	Addr        string
	Executor    *scheduler.Executor // optional; a default one is built when nil
	Workers     int
	EventBuffer int
	// ExtraSinks are attached to the executor in addition to the
	// in-memory ring buffer that backs GET /events (e.g. a JSONL file
	// sink). Ignored when a pre-built Executor is supplied.
	ExtraSinks []scheduler.EventSink
}

// New constructs the HTTP server around (optionally) a provided
// executor. When cfg.Executor is nil, a new executor is built with a
// memory sink (plus any ExtraSinks) and the requested worker count.
func New(cfg Config) (*Server, error) {
	s := &Server{events: scheduler.NewMemorySink(nonZero(cfg.EventBuffer, 4096))}
	if cfg.Executor != nil {
		s.Exec = cfg.Executor
	} else {
		opts := []scheduler.Option{scheduler.WithSinks(s.events)}
		for _, sk := range cfg.ExtraSinks {
			opts = append(opts, scheduler.WithSinks(sk))
		}
		if cfg.Workers > 0 {
			opts = append(opts, scheduler.WithWorkers(cfg.Workers))
		}
		var err error
		if s.Exec, err = scheduler.New(opts...); err != nil {
			return nil, err
		}
	}
	if cfg.Addr == "" {
		cfg.Addr = "127.0.0.1:8080"
	}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", s.handleHealth)
	mux.HandleFunc("GET /stats", s.handleStats)
	mux.HandleFunc("POST /tasks", s.handleSubmit)
	mux.HandleFunc("GET /tasks", s.handleList)
	mux.HandleFunc("GET /tasks/{id}", s.handleGet)
	mux.HandleFunc("POST /tasks/{id}/cancel", s.handleCancel)
	mux.HandleFunc("GET /events", s.handleEvents)
	mux.HandleFunc("POST /shutdown", s.handleShutdown)
	s.http = &http.Server{
		Addr:              cfg.Addr,
		Handler:           logRequests(mux),
		ReadHeaderTimeout: 5 * time.Second,
	}
	return s, nil
}

// Events exposes the ring-buffer sink (tests use it to inspect).
func (s *Server) Events() *scheduler.MemorySink { return s.events }

// Addr returns the configured listen address.
func (s *Server) Addr() string { return s.http.Addr }

// ListenAndServe starts serving.
func (s *Server) ListenAndServe() error {
	if err := s.http.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		return err
	}
	return nil
}

// Close performs a graceful HTTP shutdown then executor shutdown.
func (s *Server) Close(ctx context.Context, drainTimeout time.Duration) error {
	_ = s.http.Shutdown(ctx)
	drainCtx, cancel := context.WithTimeout(ctx, drainTimeout)
	defer cancel()
	return s.Exec.Shutdown(drainCtx)
}

type submitRequest struct {
	Kind    string          `json:"kind"`
	Payload json.RawMessage `json:"payload"`
}

type taskResponse struct {
	OK    bool               `json:"ok"`
	Task  scheduler.TaskInfo `json:"task,omitempty"`
	Error string             `json:"error,omitempty"`
}

func (s *Server) handleSubmit(w http.ResponseWriter, r *http.Request) {
	var req submitRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
		return
	}
	if strings.TrimSpace(req.Kind) == "" {
		writeError(w, http.StatusBadRequest, "field 'kind' is required")
		return
	}
	var payload any
	if len(req.Payload) > 0 {
		payload = json.RawMessage(req.Payload)
	}
	h, err := s.Exec.Submit(req.Kind, payload)
	if err != nil {
		status := http.StatusBadRequest
		if errors.Is(err, scheduler.ErrExecutorShutdown) {
			status = http.StatusServiceUnavailable
		} else if errors.Is(err, scheduler.ErrKindNotFound) {
			status = http.StatusNotFound
		}
		writeError(w, status, err.Error())
		return
	}
	info, err := s.Exec.Snapshot(h)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusAccepted, taskResponse{OK: true, Task: info})
}

func (s *Server) handleGet(w http.ResponseWriter, r *http.Request) {
	info, ok := s.findTask(w, r)
	if !ok {
		return
	}
	writeJSON(w, http.StatusOK, taskResponse{OK: true, Task: info})
}

func (s *Server) handleCancel(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	h := handleByID(s.Exec, id)
	if h == nil {
		writeError(w, http.StatusNotFound, "task not found: "+id)
		return
	}
	canceled, err := s.Exec.Cancel(h)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	info, _ := s.Exec.Snapshot(h)
	writeJSON(w, http.StatusOK, map[string]any{
		"ok":       true,
		"canceled": canceled,
		"task":     info,
	})
}

func (s *Server) handleList(w http.ResponseWriter, r *http.Request) {
	limit := 200
	if v := r.URL.Query().Get("limit"); v != "" {
		if n, err := timeconv(v); err == nil && n > 0 && n <= 5000 {
			limit = n
		}
	}
	tasks := s.Exec.ListTasks(limit)
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "tasks": tasks})
}

func (s *Server) handleStats(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "stats": s.Exec.Stats()})
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	stats := s.Exec.Stats()
	status := http.StatusOK
	if stats.ShuttingDown {
		status = http.StatusServiceUnavailable
	}
	writeJSON(w, status, map[string]any{
		"ok":          !stats.ShuttingDown,
		"workers":     stats.Workers,
		"outstanding": stats.Outstanding,
	})
}

func (s *Server) handleEvents(w http.ResponseWriter, r *http.Request) {
	var after int64
	if v := r.URL.Query().Get("after_seq"); v != "" {
		if n, err := timeconv(v); err == nil {
			after = int64(n)
		}
	}
	events, lastSeq := s.events.Since(after)
	writeJSON(w, http.StatusOK, map[string]any{
		"ok":       true,
		"last_seq": lastSeq,
		"events":   events,
	})
}

func (s *Server) handleShutdown(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusAccepted, map[string]any{"ok": true, "status": "draining"})
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		// Stop accepting HTTP connections first, then drain the
		// executor. Errors are non-fatal here: the caller already
		// received the 202 response.
		_ = s.http.Shutdown(ctx)
		_ = s.Exec.Shutdown(ctx)
	}()
}

// ---------------------------------------------------------------------------

func (s *Server) findTask(w http.ResponseWriter, r *http.Request) (scheduler.TaskInfo, bool) {
	id := r.PathValue("id")
	h := handleByID(s.Exec, id)
	if h == nil {
		writeError(w, http.StatusNotFound, "task not found: "+id)
		return scheduler.TaskInfo{}, false
	}
	info, err := s.Exec.Snapshot(h)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return scheduler.TaskInfo{}, false
	}
	return info, true
}

// handleByID looks up a task via the executor's retained task index.
// We need an accessor on the executor for a string id.
func handleByID(e *scheduler.Executor, id string) scheduler.Handle {
	return e.HandleByID(id)
}

func nonZero(v, d int) int {
	if v > 0 {
		return v
	}
	return d
}

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]any{"ok": false, "error": msg})
}
