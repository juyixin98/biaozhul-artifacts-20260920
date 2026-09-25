// Package httpapi exposes the batch aggregation scheduler over a small local
// HTTP interface: synchronous-style POST /infer (the request parks until its
// item's independent result is ready), GET /events for structured event
// streaming (SSE), plus /healthz and a root descriptor.
package httpapi

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"sync"
	"sync/atomic"
	"time"

	"batchagg"
)

// statusClientClosedRequest mirrors nginx's 499, used when the HTTP client
// disconnects before its aggregated result is ready.
const statusClientClosedRequest = 499

// statusItemCanceled marks an item removed from its pending batch by an
// explicit client cancellation before dispatch.
const statusItemCanceled = 490

// Config configures the HTTP server wrapper.
type Config struct {
	// Scheduler is the backing scheduler (required).
	Scheduler *batchagg.Scheduler
	// Logger defaults to slog.Default().
	Logger *slog.Logger
	// MaxBodyBytes caps raw request bodies. Must be >= the scheduler's
	// MaxBytes so oversized items reach the scheduler and get its 413;
	// defaults to 8 MiB.
	MaxBodyBytes int
	// EventBuffer is the per-subscriber SSE buffer; slow subscribers are
	// disconnected instead of blocking the scheduler. Defaults to 256.
	EventBuffer int
	// Broadcaster, when provided, is the SSE event bus. Pass one in so the
	// scheduler can be wired to it before it exists. When nil, the server
	// creates its own, reachable through Sink().
	Broadcaster *EventBroadcaster
}

// Server wraps a scheduler with HTTP handlers and a live event hub.
type Server struct {
	cfg     Config
	log     *slog.Logger
	mux     *http.ServeMux
	httpSrv *http.Server

	idSeq uint64
	hub   *EventBroadcaster
}

// NewServer builds the wrapper. Shut HTTP down (Shutdown) before closing the
// backing scheduler for a clean drain.
func NewServer(cfg Config) *Server {
	if cfg.Logger == nil {
		cfg.Logger = slog.Default()
	}
	if cfg.EventBuffer <= 0 {
		cfg.EventBuffer = 256
	}
	hub := cfg.Broadcaster
	if hub == nil {
		hub = NewEventBroadcaster(cfg.EventBuffer)
	}
	s := &Server{
		cfg: cfg,
		log: cfg.Logger,
		hub: hub,
	}
	if s.cfg.MaxBodyBytes <= 0 {
		s.cfg.MaxBodyBytes = 8 * 1024 * 1024
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/", s.handleRoot)
	mux.HandleFunc("GET /healthz", s.handleHealth)
	mux.HandleFunc("POST /infer", s.handleInfer)
	mux.HandleFunc("GET /events", s.handleEvents)
	s.mux = mux
	return s
}

// Sink returns a batchagg.EventSink that broadcasts events to SSE clients.
func (s *Server) Sink() batchagg.EventSink { return s.hub }

// Handler returns the http.Handler, useful with httptest.
func (s *Server) Handler() http.Handler { return s.mux }

// ListenAndServe starts serving on addr.
func (s *Server) ListenAndServe(addr string) error {
	s.httpSrv = &http.Server{
		Addr:              addr,
		Handler:           s.mux,
		ReadHeaderTimeout: 10 * time.Second,
	}
	return s.httpSrv.ListenAndServe()
}

// Shutdown gracefully stops the HTTP server and disconnects SSE clients.
func (s *Server) Shutdown(ctx context.Context) error {
	s.hub.close()
	if s.httpSrv != nil {
		return s.httpSrv.Shutdown(ctx)
	}
	return nil
}

// --- request/response types ------------------------------------------------

type inferRequest struct {
	// Key is the explicit compatibility key. Optional; when empty the
	// request's Model is used, and finally a global default key.
	Key string `json:"key,omitempty"`
	batchagg.InferRequest
}

type inferResponse struct {
	RequestID string          `json:"request_id"`
	ItemID    string          `json:"item_id"`
	BatchID   string          `json:"batch_id"`
	Index     int             `json:"index"`
	Output    json.RawMessage `json:"output"`
}

type errorResponse struct {
	Error     string `json:"error"`
	Code      string `json:"code,omitempty"`
	RequestID string `json:"request_id,omitempty"`
}

func (s *Server) newRequestID() string {
	return fmt.Sprintf("req-%d", atomic.AddUint64(&s.idSeq, 1))
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

// --- handlers --------------------------------------------------------------

func (s *Server) handleRoot(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		writeJSON(w, http.StatusNotFound, errorResponse{Error: "not found"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"service": "batch-aggregation-scheduler",
		"endpoints": []map[string]string{
			{"method": "POST", "path": "/infer", "description": "submit one inference request; aggregated by compatibility key"},
			{"method": "GET", "path": "/events", "description": "server-sent events stream of scheduler state changes"},
			{"method": "GET", "path": "/healthz", "description": "liveness probe"},
		},
	})
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *Server) handleInfer(w http.ResponseWriter, r *http.Request) {
	reqID := s.newRequestID()
	w.Header().Set("X-Request-ID", reqID)

	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, int64(s.cfg.MaxBodyBytes)))
	if err != nil {
		var mbe *http.MaxBytesError
		if errors.As(err, &mbe) {
			writeJSON(w, http.StatusRequestEntityTooLarge, errorResponse{
				Error:     "request body too large for HTTP layer",
				Code:      "http_body_too_large",
				RequestID: reqID,
			})
			return
		}
		writeJSON(w, http.StatusBadRequest, errorResponse{
			Error:     err.Error(),
			RequestID: reqID,
		})
		return
	}

	var req inferRequest
	if err := json.Unmarshal(body, &req); err != nil {
		writeJSON(w, http.StatusBadRequest, errorResponse{
			Error:     "invalid JSON: " + err.Error(),
			Code:      "bad_json",
			RequestID: reqID,
		})
		return
	}

	key := req.Key
	if key == "" {
		key = req.Model
	}
	if key == "" {
		writeJSON(w, http.StatusBadRequest, errorResponse{
			Error:     "either key or model must be provided to derive a compatibility key",
			Code:      "missing_key",
			RequestID: reqID,
		})
		return
	}
	itemID := fmt.Sprintf("%s-item", reqID)

	fut, err := s.cfg.Scheduler.Submit(r.Context(), batchagg.Item{
		ID:      itemID,
		Key:     key,
		Payload: body,
	})
	if err != nil {
		switch {
		case errors.Is(err, batchagg.ErrOversizeItem):
			writeJSON(w, http.StatusRequestEntityTooLarge, errorResponse{
				Error:     err.Error(),
				Code:      "oversize_item",
				RequestID: reqID,
			})
		case errors.Is(err, batchagg.ErrEmptyKey):
			writeJSON(w, http.StatusBadRequest, errorResponse{
				Error:     err.Error(),
				Code:      "empty_key",
				RequestID: reqID,
			})
		case errors.Is(err, batchagg.ErrClosed):
			writeJSON(w, http.StatusServiceUnavailable, errorResponse{
				Error:     err.Error(),
				Code:      "scheduler_closed",
				RequestID: reqID,
			})
		default:
			writeJSON(w, http.StatusBadRequest, errorResponse{
				Error:     err.Error(),
				RequestID: reqID,
			})
		}
		return
	}

	res, err := fut.Get(r.Context())
	if err != nil {
		// Client gave up while waiting (or its connection died). If the item
		// was still queued, Submit's cancellation hook removes just this item.
		if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
			writeJSON(w, statusClientClosedRequest, errorResponse{
				Error:     "client canceled while waiting for result: " + err.Error(),
				Code:      "client_canceled",
				RequestID: reqID,
			})
			return
		}
		writeJSON(w, http.StatusInternalServerError, errorResponse{
			Error:     err.Error(),
			RequestID: reqID,
		})
		return
	}
	if res == nil {
		// Cancellation reached the item before dispatch.
		writeJSON(w, statusItemCanceled, errorResponse{
			Error:     "request was canceled before execution",
			Code:      "item_canceled",
			RequestID: reqID,
		})
		return
	}

	if res.Err != nil {
		status := http.StatusBadGateway
		code := "item_execution_failed"
		if errors.Is(res.Err, batchagg.ErrSimulatedInference) {
			code = "simulated_inference_failure"
		}
		writeJSON(w, status, errorResponse{
			Error:     res.Err.Error(),
			Code:      code,
			RequestID: reqID,
		})
		return
	}

	writeJSON(w, http.StatusOK, inferResponse{
		RequestID: reqID,
		ItemID:    itemID,
		BatchID:   res.BatchID,
		Index:     res.Index,
		Output:    json.RawMessage(res.Output),
	})
}

func (s *Server) handleEvents(w http.ResponseWriter, r *http.Request) {
	flusher, ok := w.(http.Flusher)
	if !ok {
		writeJSON(w, http.StatusInternalServerError, errorResponse{Error: "streaming unsupported"})
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	w.Header().Set("X-Accel-Buffering", "no")
	fmt.Fprintf(w, "retry: 2000\n\n")
	flusher.Flush()

	sub := s.hub.subscribe()
	defer s.hub.unsubscribe(sub)

	keepAlive := time.NewTicker(15 * time.Second)
	defer keepAlive.Stop()

	ctx := r.Context()
	for {
		select {
		case <-ctx.Done():
			return
		case ev, open := <-sub:
			if !open {
				fmt.Fprint(w, "event: shutdown\ndata: {\"reason\":\"server_shutdown\"}\n\n")
				flusher.Flush()
				return
			}
			raw, err := json.Marshal(ev)
			if err != nil {
				continue
			}
			fmt.Fprintf(w, "event: %s\ndata: %s\n\n", ev.Type, raw)
			flusher.Flush()
		case <-keepAlive.C:
			fmt.Fprint(w, ": keep-alive\n\n")
			flusher.Flush()
		}
	}
}

// --- event hub: fan-out from scheduler sink to SSE subscribers -------------

// EventBroadcaster fans scheduler events out to SSE subscribers. It
// implements batchagg.EventSink and is safe for concurrent use. Construct it
// before the scheduler so the scheduler can emit into it directly.
type EventBroadcaster struct {
	mu      sync.RWMutex
	subs    map[chan batchagg.Event]struct{}
	bufSize int
	closed  bool
}

// NewEventBroadcaster creates a broadcaster with the given per-subscriber
// buffer size (defaults to 256 when <= 0).
func NewEventBroadcaster(bufSize int) *EventBroadcaster {
	if bufSize <= 0 {
		bufSize = 256
	}
	return &EventBroadcaster{subs: make(map[chan batchagg.Event]struct{}), bufSize: bufSize}
}

func (h *EventBroadcaster) subscribe() chan batchagg.Event {
	ch := make(chan batchagg.Event, h.bufSize)
	h.mu.Lock()
	defer h.mu.Unlock()
	if !h.closed {
		h.subs[ch] = struct{}{}
	} else {
		close(ch)
	}
	return ch
}

func (h *EventBroadcaster) unsubscribe(ch chan batchagg.Event) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if _, ok := h.subs[ch]; ok {
		delete(h.subs, ch)
		close(ch)
	}
}

// Emit implements batchagg.EventSink. It is non-blocking: a slow subscriber
// whose buffer is full is dropped and its channel closed. A single write lock
// makes send-or-drop atomic, so a channel is never closed while another Emit
// is sending on it.
func (h *EventBroadcaster) Emit(ev batchagg.Event) {
	h.mu.Lock()
	defer h.mu.Unlock()
	for ch := range h.subs {
		select {
		case ch <- ev:
		default:
			delete(h.subs, ch)
			close(ch)
		}
	}
}

func (h *EventBroadcaster) close() {
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.closed {
		return
	}
	h.closed = true
	for ch := range h.subs {
		delete(h.subs, ch)
		close(ch)
	}
}
