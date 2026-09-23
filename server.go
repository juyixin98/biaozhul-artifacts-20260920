package agingqueue

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"time"
)

// Server 把调度器暴露为本地 HTTP 接口。
type Server struct {
	sched *Scheduler
	mux   *http.ServeMux
	srv   *http.Server
}

// NewServer 构造 HTTP 处理器（路由：/healthz、/jobs、/events、/metrics）。
func NewServer(s *Scheduler) *Server {
	mux := http.NewServeMux()
	srv := &Server{sched: s, mux: mux}
	mux.HandleFunc("GET /healthz", srv.handleHealth)
	mux.HandleFunc("POST /jobs", srv.handleSubmit)
	mux.HandleFunc("GET /jobs", srv.handleList)
	mux.HandleFunc("GET /jobs/{id}", srv.handleGet)
	mux.HandleFunc("POST /jobs/{id}/cancel", srv.handleCancel)
	mux.HandleFunc("GET /events", srv.handleEvents)
	mux.HandleFunc("GET /metrics", srv.handleMetrics)
	return srv
}

// Handler 返回路由处理器（便于测试中用 httptest 挂载）。
func (s *Server) Handler() http.Handler { return s.mux }

// ListenAndServe 在 addr 上阻塞服务。
func (s *Server) ListenAndServe(addr string) error {
	s.srv = &http.Server{
		Addr:              addr,
		Handler:           s.mux,
		ReadHeaderTimeout: 5 * time.Second,
	}
	if err := s.srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		return err
	}
	return nil
}

type submitRequest struct {
	ID          string          `json:"id"`
	Type        string          `json:"type"`
	Priority    int             `json:"priority"`
	Payload     json.RawMessage `json:"payload"`
	MaxAttempts int             `json:"max_attempts"`
	BackoffMS   int64           `json:"backoff_ms"`
}

type submitResponse struct {
	Job     JobView `json:"job"`
	Outcome string  `json:"outcome"`
}

type cancelResponse struct {
	Job     JobView `json:"job"`
	Outcome string  `json:"outcome"`
}

type errorResponse struct {
	Error string `json:"error"`
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func (s *Server) handleHealth(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *Server) handleSubmit(w http.ResponseWriter, r *http.Request) {
	body, err := io.ReadAll(io.LimitReader(r.Body, 1<<20))
	if err != nil {
		writeJSON(w, http.StatusBadRequest, errorResponse{err.Error()})
		return
	}
	var req submitRequest
	if err := json.Unmarshal(body, &req); err != nil {
		writeJSON(w, http.StatusBadRequest, errorResponse{"invalid JSON: " + err.Error()})
		return
	}
	var payload []byte
	if len(req.Payload) > 0 {
		// 允许字符串或对象；统一去掉 JSON 字符串外层引号，便于 echo/sleep 消费。
		if req.Payload[0] == '"' {
			var s2 string
			if err := json.Unmarshal(req.Payload, &s2); err == nil {
				payload = []byte(s2)
			}
		} else {
			payload = req.Payload
		}
	}
	opts := SubmitOptions{
		ID:          req.ID,
		Type:        req.Type,
		Priority:    req.Priority,
		Payload:     payload,
		MaxAttempts: req.MaxAttempts,
		Backoff:     time.Duration(req.BackoffMS) * time.Millisecond,
	}
	view, err := s.sched.Submit(opts)
	if err != nil {
		status := http.StatusInternalServerError
		switch {
		case errors.Is(err, ErrEmptyType), errors.Is(err, ErrPriorityRange):
			status = http.StatusBadRequest
		case errors.Is(err, ErrUnknownExecutor):
			status = http.StatusBadRequest
		case errors.Is(err, ErrDuplicateID):
			status = http.StatusConflict
		case errors.Is(err, ErrStopped):
			status = http.StatusServiceUnavailable
		}
		writeJSON(w, status, errorResponse{err.Error()})
		return
	}
	writeJSON(w, http.StatusCreated, submitResponse{Job: *view, Outcome: "submitted"})
}

func (s *Server) handleList(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"jobs": s.sched.List()})
}

func (s *Server) handleGet(w http.ResponseWriter, r *http.Request) {
	view, err := s.sched.Get(r.PathValue("id"))
	if err != nil {
		writeJSON(w, http.StatusNotFound, errorResponse{err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"job": view})
}

func (s *Server) handleCancel(w http.ResponseWriter, r *http.Request) {
	outcome, err := s.sched.Cancel(r.PathValue("id"))
	if err != nil {
		switch {
		case errors.Is(err, ErrNotFound):
			writeJSON(w, http.StatusNotFound, errorResponse{err.Error()})
		case errors.Is(err, ErrAlreadyCanceled), errors.Is(err, ErrTerminalCancel):
			// 重复取消：对终态作业是幂等冲突，返回 409。
			writeJSON(w, http.StatusConflict, errorResponse{err.Error()})
		default:
			writeJSON(w, http.StatusInternalServerError, errorResponse{err.Error()})
		}
		return
	}
	view, _ := s.sched.Get(r.PathValue("id"))
	writeJSON(w, http.StatusOK, cancelResponse{Job: *view, Outcome: outcome})
}

func (s *Server) handleEvents(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	beforeSeq, _ := strconv.ParseInt(q.Get("before_seq"), 10, 64)
	limit, _ := strconv.Atoi(q.Get("limit"))
	events := s.sched.Events().Events(beforeSeq, limit)

	// ?stream=1 时挂为 SSE 实时事件流（测试取消等场景很有用）。
	if q.Get("stream") == "1" {
		flusher, ok := w.(http.Flusher)
		if !ok {
			writeJSON(w, http.StatusInternalServerError, errorResponse{"streaming unsupported"})
			return
		}
		w.Header().Set("Content-Type", "text/event-stream")
		w.Header().Set("Cache-Control", "no-cache")
		w.Header().Set("Connection", "keep-alive")
		enc := json.NewEncoder(w)
		for _, e := range events {
			fmt.Fprintf(w, "id: %d\n", e.Seq)
			fmt.Fprint(w, "data: ")
			_ = enc.Encode(e)
			fmt.Fprint(w, "\n\n")
		}
		flusher.Flush()
		ch, lastSeq := s.sched.Events().Subscribe()
		defer s.sched.Events().Unsubscribe(ch)
		_ = lastSeq
		ctx := r.Context()
		for {
			select {
			case <-ctx.Done():
				return
			case e, ok := <-ch:
				if !ok {
					return
				}
				fmt.Fprintf(w, "id: %d\n", e.Seq)
				fmt.Fprint(w, "data: ")
				_ = enc.Encode(e)
				fmt.Fprint(w, "\n\n")
				flusher.Flush()
			}
		}
	}

	writeJSON(w, http.StatusOK, map[string]any{"events": events})
}

func (s *Server) handleMetrics(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, s.sched.Metrics())
}
