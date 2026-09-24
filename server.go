package main

import (
	"encoding/json"
	"errors"
	"log"
	"net/http"
	"time"

	"github.com/example/gpu-placement/internal/scheduler"
	"github.com/example/gpu-placement/internal/topology"
)

// apiServer 持有调度器并负责 HTTP 编解码。业务逻辑全部在 scheduler 包。
type apiServer struct {
	sched *scheduler.Scheduler
}

func newServer() *apiServer {
	return &apiServer{sched: scheduler.New()}
}

func (s *apiServer) routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", s.handleHealth)
	mux.HandleFunc("PUT /api/cluster", s.handleConfigure)
	mux.HandleFunc("GET /api/cluster", s.handleGetCluster)
	mux.HandleFunc("POST /api/tasks", s.handleAllocate)
	mux.HandleFunc("GET /api/tasks", s.handleListTasks)
	mux.HandleFunc("GET /api/tasks/{taskID}", s.handleGetTask)
	mux.HandleFunc("DELETE /api/tasks/{taskID}", s.handleRelease)
	return loggingMiddleware(mux)
}

// ---- handlers ----

func (s *apiServer) handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"status":            "ok",
		"clusterConfigured": s.sched.HasCluster(),
	})
}

func (s *apiServer) handleConfigure(w http.ResponseWriter, r *http.Request) {
	var spec topology.Spec
	if err := decodeJSON(r, &spec); err != nil {
		writeError(w, http.StatusBadRequest, "INVALID_JSON", err.Error())
		return
	}
	if err := s.sched.Configure(spec); err != nil {
		writeError(w, http.StatusBadRequest, "INVALID_CLUSTER", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"configured": true,
		"devices":    len(spec.Devices),
		"links":      len(spec.Links),
		"view":       s.sched.View(),
	})
}

func (s *apiServer) handleGetCluster(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, s.sched.View())
}

func (s *apiServer) handleAllocate(w http.ResponseWriter, r *http.Request) {
	var req scheduler.TaskRequest
	if err := decodeJSON(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, "INVALID_JSON", err.Error())
		return
	}
	dec := s.sched.Allocate(req)
	switch {
	case dec.Place != nil:
		writeJSON(w, http.StatusCreated, dec.Place)
	case dec.Reject != nil:
		// 业务上“能理解请求但无法放置”统一用 422，拒绝原因结构化返回；
		// 参数错误仍用 400；未配置集群用 409。
		switch dec.Reject.Reason {
		case scheduler.ReasonInvalidTask:
			writeJSON(w, http.StatusBadRequest, dec.Reject)
		case scheduler.ReasonNoCluster:
			writeJSON(w, http.StatusConflict, dec.Reject)
		default:
			writeJSON(w, http.StatusUnprocessableEntity, dec.Reject)
		}
	default:
		writeError(w, http.StatusInternalServerError, "INTERNAL", "调度器返回了空决策")
	}
}

func (s *apiServer) handleListTasks(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"tasks": s.sched.Tasks()})
}

func (s *apiServer) handleGetTask(w http.ResponseWriter, r *http.Request) {
	taskID := r.PathValue("taskID")
	p, ok := s.sched.Task(taskID)
	if !ok {
		writeError(w, http.StatusNotFound, "TASK_NOT_FOUND", "任务不存在: "+taskID)
		return
	}
	writeJSON(w, http.StatusOK, p)
}

func (s *apiServer) handleRelease(w http.ResponseWriter, r *http.Request) {
	taskID := r.PathValue("taskID")
	if !s.sched.Release(taskID) {
		writeError(w, http.StatusNotFound, "TASK_NOT_FOUND", "任务不存在: "+taskID)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"released": taskID})
}

// ---- helpers ----

// statusRecorder 记录状态码供访问日志使用。
type statusRecorder struct {
	http.ResponseWriter
	status int
}

func (r *statusRecorder) WriteHeader(code int) {
	r.status = code
	r.ResponseWriter.WriteHeader(code)
}

// loggingMiddleware 记录每个请求的方法、路径、状态码与耗时。
func loggingMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		rec := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(rec, r)
		log.Printf("%s %s -> %d (%s)", r.Method, r.URL.Path, rec.status, time.Since(start))
	})
}

func decodeJSON(r *http.Request, dst any) error {
	// 1MiB 上限，避免异常大请求体。
	r.Body = http.MaxBytesReader(nil, r.Body, 1<<20)
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(dst); err != nil {
		return errors.New("请求体不是合法 JSON（超过 1MiB 或含未知字段也会被拒绝）: " + err.Error())
	}
	return nil
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

func writeError(w http.ResponseWriter, status int, code, msg string) {
	writeJSON(w, status, map[string]any{"error": map[string]string{
		"code":    code,
		"message": msg,
	}})
}
