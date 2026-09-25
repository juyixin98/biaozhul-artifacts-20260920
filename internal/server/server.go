// Package server exposes dynpool pools over a small local HTTP API.
//
// Endpoints (v1):
//
//	POST   /v1/pools                          create a named pool
//	GET    /v1/pools                          list pool names + status
//	GET    /v1/pools/{name}                   pool status
//	DELETE /v1/pools/{name}                   graceful shutdown (query ?force=1 for force)
//	POST   /v1/pools/{name}/tasks             submit a task (sleep / block / echo)
//	GET    /v1/pools/{name}/tasks             list accepted task ids
//	GET    /v1/pools/{name}/tasks/{id}        task result/state
//	POST   /v1/pools/{name}/workers           resize worker count
//	GET    /v1/pools/{name}/events            recent events (query ?from=&type=)
//	POST   /v1/blocks/{name}/release          release a named blocking task
//
// Task types usable over HTTP: "sleep" (sleep ms), "block" (parks until
// released via /v1/blocks), "echo" (returns payload).
package server

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"sync"

	"dynpool"
)

// Server holds the set of named pools.
type Server struct {
	mu    sync.RWMutex
	pools map[string]*entry

	// events stores recent structured events per pool (ring-free bounded
	// slice: a local demo interface, keep it simple).
	historyLen int
}

type entry struct {
	pool    *dynpool.Pool
	sink    *recordingSink
	blocks  sync.Map // block task name -> chan struct{}
	results sync.Map // task id -> echo payload (demo task results)
}

// New creates an empty server. historyLen sets the per-pool event retention.
func New(historyLen int) *Server {
	if historyLen <= 0 {
		historyLen = 1024
	}
	return &Server{pools: map[string]*entry{}, historyLen: historyLen}
}

type recordingSink struct {
	mu     sync.Mutex
	events []dynpool.Event
	limit  int
}

func (s *recordingSink) OnEvent(e dynpool.Event) {
	s.mu.Lock()
	s.events = append(s.events, e)
	if len(s.events) > s.limit {
		s.events = s.events[len(s.events)-s.limit:]
	}
	s.mu.Unlock()
}

func (s *recordingSink) snapshot(from int64, typ string) []dynpool.Event {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]dynpool.Event, 0, len(s.events))
	for _, e := range s.events {
		if e.Seq < from {
			continue
		}
		if typ != "" && string(e.Type) != typ {
			continue
		}
		out = append(out, e)
	}
	return out
}

// Handler returns the HTTP mux.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /v1/pools", s.handleCreatePool)
	mux.HandleFunc("GET /v1/pools", s.handleListPools)
	mux.HandleFunc("GET /v1/pools/{name}", s.handleGetPool)
	mux.HandleFunc("DELETE /v1/pools/{name}", s.handleDeletePool)
	mux.HandleFunc("POST /v1/pools/{name}/workers", s.handleResize)
	mux.HandleFunc("POST /v1/pools/{name}/tasks", s.handleSubmit)
	mux.HandleFunc("GET /v1/pools/{name}/tasks", s.handleListTasks)
	mux.HandleFunc("GET /v1/pools/{name}/tasks/{id}", s.handleGetTask)
	mux.HandleFunc("GET /v1/pools/{name}/events", s.handleEvents)
	mux.HandleFunc("POST /v1/blocks/{name}/release", s.handleReleaseBlock)
	return mux
}

// ---------- request/response DTOs ----------

type createPoolReq struct {
	Name        string `json:"name"`
	Workers     int    `json:"workers"`
	QueueSize   int    `json:"queue_size"`
	Reject      string `json:"reject_policy"`
	GracePeriod string `json:"grace_period"`
}

type resizeReq struct {
	Workers int `json:"workers"`
}

type taskReq struct {
	Type    string `json:"type"` // sleep | block | echo
	ID      string `json:"id"`   // optional caller-chosen id
	SleepMs int    `json:"sleep_ms"`
	Name    string `json:"name"`    // block task name (release key)
	Payload string `json:"payload"` // echo payload
}

type taskResp struct {
	ID       string `json:"id"`
	State    string `json:"state"` // queued | running | completed | rejected | dropped
	Ran      bool   `json:"ran"`
	Error    string `json:"error,omitempty"`
	Result   string `json:"result,omitempty"`
	Rejected bool   `json:"rejected,omitempty"`
}

type errResp struct {
	Error string `json:"error"`
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, code int, format string, args ...any) {
	writeJSON(w, code, errResp{Error: fmt.Sprintf(format, args...)})
}

// ---------- handlers ----------

func (s *Server) handleCreatePool(w http.ResponseWriter, r *http.Request) {
	var req createPoolReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid json: %v", err)
		return
	}
	name := strings.TrimSpace(req.Name)
	if name == "" {
		writeErr(w, http.StatusBadRequest, "name required")
		return
	}
	cfg := dynpool.Config{
		Name:      name,
		Workers:   req.Workers,
		QueueSize: req.QueueSize,
		Reject:    dynpool.RejectPolicy(req.Reject),
	}
	if req.GracePeriod != "" {
		d, err := parseDuration(req.GracePeriod)
		if err != nil {
			writeErr(w, http.StatusBadRequest, "bad grace_period: %v", err)
			return
		}
		cfg.GracePeriod = d
	}
	sink := &recordingSink{limit: s.historyLen}
	cfg.Sink = sink
	p, err := dynpool.New(cfg)
	if err != nil {
		writeErr(w, http.StatusBadRequest, "%v", err)
		return
	}
	e := &entry{pool: p, sink: sink}
	s.mu.Lock()
	if _, exists := s.pools[name]; exists {
		s.mu.Unlock()
		_ = p.ShutdownNow()
		writeErr(w, http.StatusConflict, "pool %q already exists", name)
		return
	}
	s.pools[name] = e
	s.mu.Unlock()
	writeJSON(w, http.StatusCreated, p.Status())
}

func (s *Server) get(w http.ResponseWriter, name string) (*entry, bool) {
	s.mu.RLock()
	e, ok := s.pools[name]
	s.mu.RUnlock()
	if !ok {
		writeErr(w, http.StatusNotFound, "pool %q not found", name)
		return nil, false
	}
	return e, true
}

func (s *Server) handleListPools(w http.ResponseWriter, r *http.Request) {
	s.mu.RLock()
	names := make([]string, 0, len(s.pools))
	entries := make([]*entry, 0, len(s.pools))
	for name, e := range s.pools {
		names = append(names, name)
		entries = append(entries, e)
	}
	s.mu.RUnlock()
	out := make([]dynpool.Status, 0, len(entries))
	for _, e := range entries {
		out = append(out, e.pool.Status())
	}
	writeJSON(w, http.StatusOK, map[string]any{"pools": out})
}

func (s *Server) handleGetPool(w http.ResponseWriter, r *http.Request) {
	e, ok := s.get(w, r.PathValue("name"))
	if !ok {
		return
	}
	writeJSON(w, http.StatusOK, e.pool.Status())
}

func (s *Server) handleDeletePool(w http.ResponseWriter, r *http.Request) {
	e, ok := s.get(w, r.PathValue("name"))
	if !ok {
		return
	}
	if r.URL.Query().Get("force") == "1" {
		dropped := e.pool.ShutdownNow()
		writeJSON(w, http.StatusOK, map[string]any{
			"status":  e.pool.Status(),
			"dropped": dropped,
		})
		return
	}
	if err := e.pool.Shutdown(r.Context()); err != nil {
		writeErr(w, http.StatusRequestTimeout, "graceful shutdown not finished: %v", err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"status": e.pool.Status()})
}

func (s *Server) handleResize(w http.ResponseWriter, r *http.Request) {
	e, ok := s.get(w, r.PathValue("name"))
	if !ok {
		return
	}
	var req resizeReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid json: %v", err)
		return
	}
	if err := e.pool.Resize(req.Workers); err != nil {
		status := http.StatusBadRequest
		if errors.Is(err, dynpool.ErrPoolShuttingDown) || errors.Is(err, dynpool.ErrPoolStopped) {
			status = http.StatusConflict
		}
		writeErr(w, status, "%v", err)
		return
	}
	writeJSON(w, http.StatusOK, e.pool.Status())
}

func (s *Server) handleListTasks(w http.ResponseWriter, r *http.Request) {
	e, ok := s.get(w, r.PathValue("name"))
	if !ok {
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"tasks": e.pool.TaskIDs()})
}

func (s *Server) handleGetTask(w http.ResponseWriter, r *http.Request) {
	e, ok := s.get(w, r.PathValue("name"))
	if !ok {
		return
	}
	id := r.PathValue("id")
	h, ok2 := e.pool.Handle(id)
	if !ok2 {
		writeErr(w, http.StatusNotFound, "task %q not found", id)
		return
	}
	resp := taskResp{ID: id}
	if !h.IsDone() {
		resp.State = "pending"
		writeJSON(w, http.StatusOK, resp)
		return
	}
	ran, err := h.Result()
	resp.Ran = ran
	if ran {
		resp.State = "completed"
		if v, ok := e.results.Load(id); ok {
			resp.Result = v.(string)
		}
	} else {
		resp.State = "discarded"
	}
	if err != nil {
		resp.Error = err.Error()
	}
	writeJSON(w, http.StatusOK, resp)
}

func (s *Server) handleEvents(w http.ResponseWriter, r *http.Request) {
	e, ok := s.get(w, r.PathValue("name"))
	if !ok {
		return
	}
	var from int64
	if v := r.URL.Query().Get("from"); v != "" {
		if _, err := fmt.Sscanf(v, "%d", &from); err != nil {
			writeErr(w, http.StatusBadRequest, "bad from: %v", err)
			return
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"events": e.sink.snapshot(from, r.URL.Query().Get("type")),
	})
}
