// Package server 提供批处理聚合调度器的本地 HTTP 接口：
//
//	POST /v1/infer        提交一个推理请求（阻塞至其所属批次执行完毕）
//	GET  /v1/events       SSE 实时订阅调度事件
//	GET  /v1/metrics      计数器与发批原因的 JSON 快照
//	GET  /healthz         存活检查
package server

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"sync/atomic"
	"time"

	"batchagg/batch"
	"batchagg/inference"
)

// Server 持有 HTTP 服务及其调度器。
type Server struct {
	batcher   *batch.Batcher[*inference.Request]
	exec      *inference.FakeExecutor
	broadcast *Broadcaster
	httpSrv   *http.Server
	listenErr chan error
	maxBody   int
	requests  atomic.Int64
}

// Config 为 HTTP 服务配置（调度参数直接透传给 batch.Config）。
type Config struct {
	Addr          string
	MaxCount      int
	MaxBatchBytes int
	MaxItemBytes  int
	MaxWait       time.Duration
	ExecLatency   time.Duration
	Clock         batch.Clock
	// ExtraSink 可选：除 SSE 广播外再输出一份事件（如结构化日志）。
	ExtraSink batch.EventSink
}

// New 构造服务。返回的服务尚未开始监听，需调用 Start/Serve。
func New(cfg Config) (*Server, error) {
	bc := &Broadcaster{clients: make(map[uint64]chan batch.Event), bufSize: 256}
	sinks := batch.MultiSink{bc}
	if cfg.ExtraSink != nil {
		sinks = append(sinks, cfg.ExtraSink)
	}

	exec := &inference.FakeExecutor{Latency: cfg.ExecLatency}
	b, err := batch.New[*inference.Request](exec, batch.Config{
		MaxCount:      cfg.MaxCount,
		MaxBatchBytes: cfg.MaxBatchBytes,
		MaxItemBytes:  cfg.MaxItemBytes,
		MaxWait:       cfg.MaxWait,
		Clock:         cfg.Clock,
		Sink:          sinks,
	})
	if err != nil {
		return nil, err
	}

	s := &Server{
		batcher:   b,
		exec:      exec,
		broadcast: bc,
		listenErr: make(chan error, 1),
		maxBody:   cfg.MaxItemBytes + 4096, // 允许 JSON 外壳开销
	}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", s.handleHealth)
	mux.HandleFunc("POST /v1/infer", s.handleInfer)
	mux.HandleFunc("GET /v1/events", s.handleEvents)
	mux.HandleFunc("GET /v1/metrics", s.handleMetrics)
	mux.HandleFunc("GET /{$}", s.handleIndex)

	s.httpSrv = &http.Server{
		Addr:              cfg.Addr,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
	}
	return s, nil
}

// ListenErr 返回后台监听期间产生的错误（如端口被占用）。
// 成功关闭后该通道收到 nil。
func (s *Server) ListenErr() <-chan error { return s.listenErr }

// Start 在后台监听并服务；监听失败可通过 ListenErr 感知。
func (s *Server) Start() {
	go func() {
		err := s.httpSrv.ListenAndServe()
		if errors.Is(err, http.ErrServerClosed) {
			err = nil
		}
		s.listenErr <- err
	}()
}

// Shutdown 优雅关闭 HTTP 与调度器，等待在途请求处理完毕。
func (s *Server) Shutdown(ctx context.Context) error {
	if err := s.httpSrv.Shutdown(ctx); err != nil {
		return err
	}
	return s.batcher.Shutdown(ctx)
}

// Batcher 暴露底层调度器，便于测试或嵌入。
func (s *Server) Batcher() *batch.Batcher[*inference.Request] { return s.batcher }

// Executor 暴露模拟执行器，便于测试断言。
func (s *Server) Executor() *inference.FakeExecutor { return s.exec }

func (s *Server) handleHealth(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *Server) handleIndex(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	writeJSON(w, http.StatusOK, map[string]any{
		"service": "batch-aggregation-scheduler",
		"endpoints": map[string]string{
			"POST /v1/infer":   "submit one inference request",
			"GET  /v1/events":  "SSE stream of scheduler events",
			"GET  /v1/metrics": "scheduler counters",
			"GET  /healthz":    "liveness",
		},
	})
}

type inferRequest struct {
	ID            string `json:"id"`
	Model         string `json:"model"`
	Prompt        string `json:"prompt"`
	SimulateError string `json:"simulate_error,omitempty"`
}

type inferResponse struct {
	RequestID string         `json:"request_id"`
	Status    string         `json:"status"` // "ok" | "item_error" | "error"
	Result    map[string]any `json:"result,omitempty"`
	Error     *respError     `json:"error,omitempty"`
}

type respError struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

func (s *Server) handleInfer(w http.ResponseWriter, r *http.Request) {
	body := http.MaxBytesReader(w, r.Body, int64(s.maxBody))
	raw, err := io.ReadAll(body)
	if err != nil {
		var mbe *http.MaxBytesError
		if errors.As(err, &mbe) {
			writeJSON(w, http.StatusRequestEntityTooLarge, inferResponse{
				Status: "error",
				Error:  &respError{Code: "request_too_large", Message: err.Error()},
			})
			return
		}
		writeJSON(w, http.StatusBadRequest, inferResponse{
			Status: "error",
			Error:  &respError{Code: "bad_request", Message: err.Error()},
		})
		return
	}

	var req inferRequest
	if err := json.Unmarshal(raw, &req); err != nil {
		writeJSON(w, http.StatusBadRequest, inferResponse{
			Status: "error",
			Error:  &respError{Code: "invalid_json", Message: err.Error()},
		})
		return
	}
	if req.Model == "" {
		writeJSON(w, http.StatusBadRequest, inferResponse{
			Status: "error",
			Error:  &respError{Code: "model_required", Message: "field 'model' is required"},
		})
		return
	}
	if req.Prompt == "" {
		writeJSON(w, http.StatusBadRequest, inferResponse{
			Status: "error",
			Error:  &respError{Code: "prompt_required", Message: "field 'prompt' is required"},
		})
		return
	}
	if req.ID == "" {
		req.ID = fmt.Sprintf("auto-%d", s.requests.Add(1))
	}

	item := &inference.Request{
		ID:            req.ID,
		Model:         req.Model,
		Prompt:        req.Prompt,
		SimulateError: req.SimulateError,
	}

	out, err := s.batcher.Submit(r.Context(), item)
	if err != nil {
		s.writeSubmitError(w, req.ID, err)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	writeJSON(w, http.StatusOK, inferResponse{
		RequestID: req.ID,
		Status:    "ok",
		Result:    out,
	})
}

func (s *Server) writeSubmitError(w http.ResponseWriter, reqID string, err error) {
	resp := inferResponse{RequestID: reqID}
	status := http.StatusInternalServerError

	var tooLarge *batch.ItemTooLargeError
	var itemErr *inference.ItemError
	switch {
	case errors.As(err, &tooLarge):
		status = http.StatusRequestEntityTooLarge
		resp.Status = "error"
		resp.Error = &respError{
			Code:    "item_too_large",
			Message: err.Error(),
		}
	case errors.As(err, &itemErr):
		// 部分失败：单项失败本身不影响其它请求，以 200 + item_error 表达，
		// 便于客户端按业务字段判定而不触发 HTTP 重试。
		status = http.StatusOK
		resp.Status = "item_error"
		resp.Error = &respError{Code: itemErr.Code, Message: itemErr.Message}
	case errors.Is(err, batch.ErrShuttingDown):
		status = http.StatusServiceUnavailable
		resp.Status = "error"
		resp.Error = &respError{Code: "shutting_down", Message: err.Error()}
	case errors.Is(err, context.Canceled):
		status = 499
		resp.Status = "error"
		resp.Error = &respError{Code: "canceled", Message: err.Error()}
	default:
		resp.Status = "error"
		resp.Error = &respError{Code: "batch_executor_failed", Message: err.Error()}
	}
	w.Header().Set("Content-Type", "application/json")
	writeJSON(w, status, resp)
}

func (s *Server) handleEvents(w http.ResponseWriter, r *http.Request) {
	flusher, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "streaming unsupported", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	w.WriteHeader(http.StatusOK)

	events, cancel := s.broadcast.Subscribe()
	defer cancel()

	// 连接建立事件，便于客户端确认流已就绪。
	fmt.Fprintf(w, "event: ready\ndata: {}\n\n")
	flusher.Flush()

	ctx := r.Context()
	for {
		select {
		case <-ctx.Done():
			return
		case ev, ok := <-events:
			if !ok {
				return
			}
			data, _ := json.Marshal(ev)
			fmt.Fprintf(w, "event: %s\ndata: %s\n\n", ev.Type, data)
			flusher.Flush()
		}
	}
}

func (s *Server) handleMetrics(w http.ResponseWriter, _ *http.Request) {
	st := s.batcher.Stats()
	w.Header().Set("Content-Type", "application/json")
	writeJSON(w, http.StatusOK, map[string]any{
		"scheduler": st,
		"executor": map[string]any{
			"batches_executed": s.exec.BatchCount(),
			"batch_sizes":      s.exec.BatchSizes(),
		},
	})
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	_ = enc.Encode(v)
}
