// Package server 提供工作窃取执行器的本地 HTTP 管理接口（纯标准库）。
//
// 它不是前端：只暴露 JSON/SSE 接口，用于提交任务、查询状态、取消任务、
// 订阅结构化事件流、管理执行器生命周期。
package server

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"worksteal/internal/clock"
	"worksteal/internal/event"
	"worksteal/internal/executor"
	"worksteal/internal/scheduler"
)

// Server 管理多个命名执行器及对应的调度器。
type Server struct {
	log     *slog.Logger
	mu      sync.RWMutex
	managed map[string]*managed
}

type managed struct {
	ex        *executor.Executor
	sched     *scheduler.Scheduler
	tasks     sync.Map // id -> *executor.Task（含已终结，便于事后查询）
	schedules sync.Map // id -> *scheduler.Handle
}

// New 创建空服务器。
func New(log *slog.Logger) *Server {
	if log == nil {
		log = slog.Default()
	}
	return &Server{log: log, managed: make(map[string]*managed)}
}

// Close 强制关停所有仍受管的执行器与调度器（进程退出时使用）。
func (s *Server) Close() {
	s.mu.Lock()
	ms := make([]*managed, 0, len(s.managed))
	for _, m := range s.managed {
		ms = append(ms, m)
	}
	s.managed = make(map[string]*managed)
	s.mu.Unlock()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	for _, m := range ms {
		m.sched.Shutdown(ctx)
		m.ex.ShutdownNow()
	}
}

// CreateExecutor 创建并登记一个执行器。
func (s *Server) CreateExecutor(name string, workers int, dequeKind string) (*managed, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.managed[name]; ok {
		return nil, fmt.Errorf("executor %q already exists", name)
	}
	ex := executor.New(executor.Config{
		Name:      name,
		Workers:   workers,
		DequeKind: dequeKind,
	})
	m := &managed{ex: ex, sched: scheduler.New(ex, clock.NewReal())}
	s.managed[name] = m
	return m, nil
}

// get 取执行器（读锁）。
func (s *Server) get(name string) (*managed, bool) {
	s.mu.RLock()
	m, ok := s.managed[name]
	s.mu.RUnlock()
	return m, ok
}

// Handler 构建路由。
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", s.handleHealth)
	mux.HandleFunc("GET /api/executors", s.handleList)
	mux.HandleFunc("POST /api/executors", s.handleCreate)
	mux.HandleFunc("GET /api/executors/{name}", s.handleStats)
	mux.HandleFunc("DELETE /api/executors/{name}", s.handleDelete)
	mux.HandleFunc("POST /api/executors/{name}/tasks", s.handleSubmit)
	mux.HandleFunc("GET /api/executors/{name}/tasks", s.handleTasks)
	mux.HandleFunc("GET /api/executors/{name}/tasks/{id}", s.handleTask)
	mux.HandleFunc("POST /api/executors/{name}/tasks/{id}/cancel", s.handleCancel)
	mux.HandleFunc("POST /api/executors/{name}/schedules", s.handleSchedule)
	mux.HandleFunc("DELETE /api/executors/{name}/schedules/{id}", s.handleScheduleCancel)
	mux.HandleFunc("GET /api/executors/{name}/events", s.handleEvents)
	return logRequests(s.log, mux)
}

func logRequests(log *slog.Logger, h http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		h.ServeHTTP(w, r)
		log.Debug("http", "method", r.Method, "path", r.URL.Path, "dur", time.Since(start))
	})
}

// ---------- 请求/响应类型 ----------

type createReq struct {
	Name    string `json:"name"`
	Workers int    `json:"workers"`
	Deque   string `json:"deque"` // chaselev | mutex
}

type submitReq struct {
	// Type: noop | sleep | compute | tree
	Type   string         `json:"type"`
	Name   string         `json:"name"`
	Params map[string]any `json:"params"`
}

type scheduleReq struct {
	Name    string    `json:"name"`
	AfterMS int64     `json:"after_ms"`
	EveryMS int64     `json:"every_ms"`
	Task    submitReq `json:"task"`
}

type taskResp struct {
	executor.Snapshot
}

// ---------- handlers ----------

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *Server) handleList(w http.ResponseWriter, r *http.Request) {
	s.mu.RLock()
	names := make([]string, 0, len(s.managed))
	for name := range s.managed {
		names = append(names, name)
	}
	s.mu.RUnlock()
	writeJSON(w, http.StatusOK, map[string]any{"executors": names})
}

func (s *Server) handleCreate(w http.ResponseWriter, r *http.Request) {
	var req createReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid json: "+err.Error())
		return
	}
	if req.Name == "" {
		writeErr(w, http.StatusBadRequest, "name required")
		return
	}
	if req.Deque == "" {
		req.Deque = "chaselev"
	}
	if req.Deque != "chaselev" && req.Deque != "mutex" {
		writeErr(w, http.StatusBadRequest, "deque must be chaselev or mutex")
		return
	}
	if _, err := s.CreateExecutor(req.Name, req.Workers, req.Deque); err != nil {
		writeErr(w, http.StatusConflict, err.Error())
		return
	}
	s.log.Info("executor created", "name", req.Name, "workers", req.Workers, "deque", req.Deque)
	writeJSON(w, http.StatusCreated, map[string]any{
		"name": req.Name, "workers": effectiveWorkers(req.Workers), "deque": req.Deque,
	})
}

func (s *Server) handleStats(w http.ResponseWriter, r *http.Request) {
	m, ok := s.get(r.PathValue("name"))
	if !ok {
		writeErr(w, http.StatusNotFound, "executor not found")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"name":      m.ex.Name(),
		"workers":   m.ex.Workers(),
		"stats":     m.ex.Stats(),
		"schedules": mapLen(&m.schedules),
	})
}

func (s *Server) handleDelete(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("name")
	s.mu.Lock()
	m, ok := s.managed[name]
	if !ok {
		s.mu.Unlock()
		writeErr(w, http.StatusNotFound, "executor not found")
		return
	}
	delete(s.managed, name)
	s.mu.Unlock()

	mode := r.URL.Query().Get("mode")
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()
	if mode == "now" {
		pending := m.ex.ShutdownNow()
		m.sched.Shutdown(ctx)
		writeJSON(w, http.StatusOK, map[string]any{"shutdown": "now", "pending": pending})
		return
	}
	if err := m.ex.Shutdown(ctx); err != nil {
		m.sched.Shutdown(ctx)
		writeJSON(w, http.StatusOK, map[string]any{"shutdown": "graceful", "wait": err.Error()})
		return
	}
	m.sched.Shutdown(ctx)
	writeJSON(w, http.StatusOK, map[string]any{"shutdown": "graceful"})
}

func (s *Server) handleSubmit(w http.ResponseWriter, r *http.Request) {
	m, ok := s.get(r.PathValue("name"))
	if !ok {
		writeErr(w, http.StatusNotFound, "executor not found")
		return
	}
	var req submitReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid json: "+err.Error())
		return
	}
	fn, opts, err := buildTaskFunc(req)
	if err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}
	t, err := m.ex.Submit(fn, opts...)
	if err != nil {
		writeErr(w, http.StatusServiceUnavailable, err.Error())
		return
	}
	m.tasks.Store(t.ID(), t)
	writeJSON(w, http.StatusAccepted, t.Snapshot())
}

func (s *Server) handleTasks(w http.ResponseWriter, r *http.Request) {
	m, ok := s.get(r.PathValue("name"))
	if !ok {
		writeErr(w, http.StatusNotFound, "executor not found")
		return
	}
	// 优先返回执行器内在册（未终结）任务；附带外部提交任务的历史状态。
	live := m.ex.Snapshots()
	historical := []executor.Snapshot{}
	m.tasks.Range(func(_, v any) bool {
		t := v.(*executor.Task)
		if t.Status().Terminal() {
			historical = append(historical, t.Snapshot())
		}
		return true
	})
	writeJSON(w, http.StatusOK, map[string]any{"live": live, "terminal": historical})
}

func (s *Server) handleTask(w http.ResponseWriter, r *http.Request) {
	m, ok := s.get(r.PathValue("name"))
	if !ok {
		writeErr(w, http.StatusNotFound, "executor not found")
		return
	}
	id := r.PathValue("id")
	if v, ok := m.tasks.Load(id); ok {
		writeJSON(w, http.StatusOK, v.(*executor.Task).Snapshot())
		return
	}
	if t, ok := m.ex.Get(id); ok {
		writeJSON(w, http.StatusOK, t.Snapshot())
		return
	}
	writeErr(w, http.StatusNotFound, "task not found (may have been evicted)")
}

func (s *Server) handleCancel(w http.ResponseWriter, r *http.Request) {
	m, ok := s.get(r.PathValue("name"))
	if !ok {
		writeErr(w, http.StatusNotFound, "executor not found")
		return
	}
	id := r.PathValue("id")
	if !m.ex.Cancel(id, errors.New("canceled via http")) {
		writeErr(w, http.StatusNotFound, "task not found or already terminal")
		return
	}
	if v, ok := m.tasks.Load(id); ok {
		writeJSON(w, http.StatusOK, v.(*executor.Task).Snapshot())
		return
	}
	writeJSON(w, http.StatusAccepted, map[string]string{"id": id, "status": "cancel_requested"})
}

func (s *Server) handleSchedule(w http.ResponseWriter, r *http.Request) {
	m, ok := s.get(r.PathValue("name"))
	if !ok {
		writeErr(w, http.StatusNotFound, "executor not found")
		return
	}
	var req scheduleReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid json: "+err.Error())
		return
	}
	fn, _, err := buildTaskFunc(req.Task)
	if err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}
	var h *scheduler.Handle
	switch {
	case req.EveryMS > 0:
		h, err = m.sched.Every(req.Name, time.Duration(req.EveryMS)*time.Millisecond, fn)
	case req.AfterMS >= 0:
		h, err = m.sched.After(req.Name, time.Duration(req.AfterMS)*time.Millisecond, fn)
	default:
		writeErr(w, http.StatusBadRequest, "after_ms or every_ms required")
		return
	}
	if err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}
	m.schedules.Store(h.ID(), h)
	writeJSON(w, http.StatusCreated, map[string]any{
		"id": h.ID(), "name": h.Name(), "periodic": h.Periodic(),
		"after_ms": req.AfterMS, "every_ms": req.EveryMS,
	})
}

func (s *Server) handleScheduleCancel(w http.ResponseWriter, r *http.Request) {
	m, ok := s.get(r.PathValue("name"))
	if !ok {
		writeErr(w, http.StatusNotFound, "executor not found")
		return
	}
	id := r.PathValue("id")
	v, ok := m.schedules.LoadAndDelete(id)
	if !ok {
		writeErr(w, http.StatusNotFound, "schedule not found")
		return
	}
	v.(*scheduler.Handle).Cancel()
	writeJSON(w, http.StatusOK, map[string]string{"id": id, "status": "canceled"})
}

func (s *Server) handleEvents(w http.ResponseWriter, r *http.Request) {
	m, ok := s.get(r.PathValue("name"))
	if !ok {
		writeErr(w, http.StatusNotFound, "executor not found")
		return
	}
	flusher, ok := w.(http.Flusher)
	if !ok {
		writeErr(w, http.StatusInternalServerError, "streaming unsupported")
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	w.Header().Set("X-Accel-Buffering", "no")

	sub, history := m.ex.Bus().Subscribe(256)
	defer sub.Close()
	enc := json.NewEncoder(w)
	write := func(ev event.Event) error {
		if _, err := fmt.Fprintf(w, "event: %s\ndata: ", ev.Kind); err != nil {
			return err
		}
		if err := enc.Encode(ev); err != nil {
			return err
		}
		_, err := fmt.Fprint(w, "\n\n")
		return err
	}
	for _, ev := range history {
		if err := write(ev); err != nil {
			return
		}
	}
	flusher.Flush()
	// 心跳，保持连接。
	ticker := time.NewTicker(15 * time.Second)
	defer ticker.Stop()
	ctx := r.Context()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if _, err := fmt.Fprint(w, ": ping\n\n"); err != nil {
				return
			}
			flusher.Flush()
		case ev, ok := <-sub.C():
			if !ok {
				return
			}
			if err := write(ev); err != nil {
				return
			}
			flusher.Flush()
		}
	}
}

// ---------- 辅助 ----------

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, code int, msg string) {
	writeJSON(w, code, map[string]string{"error": msg})
}

func mapLen(m *sync.Map) int {
	n := 0
	m.Range(func(_, _ any) bool { n++; return true })
	return n
}

func effectiveWorkers(n int) int {
	if n > 0 {
		return n
	}
	return runtimeNumCPU()
}

// 供 params 取整型。
func paramInt(p map[string]any, key string, def int) int {
	if p == nil {
		return def
	}
	v, ok := p[key]
	if !ok {
		return def
	}
	switch x := v.(type) {
	case float64:
		return int(x)
	case int:
		return x
	case string:
		if n, err := strconv.Atoi(strings.TrimSpace(x)); err == nil {
			return n
		}
	}
	return def
}

func paramString(p map[string]any, key, def string) string {
	if p == nil {
		return def
	}
	if v, ok := p[key].(string); ok && v != "" {
		return v
	}
	return def
}
