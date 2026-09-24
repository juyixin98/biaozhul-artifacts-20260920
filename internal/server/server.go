// Package server wires the broker to HTTP: publishing, SSE streaming with
// Last-Event-ID resume, bounded replay queries and observability.
package server

import (
	"encoding/json"
	"errors"
	"io"
	"log"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"sse-resume/internal/broker"
	"sse-resume/internal/sse"
)

// Server holds HTTP handlers.
type Server struct {
	br             *broker.Broker
	heartbeat      time.Duration
	writeTimeout   time.Duration
	clientBodySize int64
	gate           *flushGate // tests only; nil = always pass
	mux            *http.ServeMux
}

// flushGate lets tests freeze SSE flushes mid-stream, simulating a stalled
// peer whose TCP window is exhausted. It is unexported and wired only from
// in-package tests.
type flushGate struct {
	mu     sync.Mutex
	resume chan struct{} // nil = open; closed channel is replaced on unblock
}

func newFlushGate() *flushGate { return &flushGate{} }

// wait blocks while the gate is closed.
func (g *flushGate) wait() {
	if g == nil {
		return
	}
	g.mu.Lock()
	ch := g.resume
	g.mu.Unlock()
	if ch != nil {
		<-ch
	}
}

// block closes traffic through wait(); a later resume releases waiters.
func (g *flushGate) block() {
	g.mu.Lock()
	if g.resume == nil {
		g.resume = make(chan struct{})
	}
	g.mu.Unlock()
}

func (g *flushGate) resumeFlushes() {
	g.mu.Lock()
	ch := g.resume
	g.resume = nil
	g.mu.Unlock()
	if ch != nil {
		close(ch)
	}
}

// Option configures a Server.
type Option func(*Server)

// WithHeartbeat sets the keep-alive comment interval (default 15s).
func WithHeartbeat(d time.Duration) Option {
	return func(s *Server) { s.heartbeat = d }
}

// WithWriteTimeout sets the per-frame write deadline used to evict stalled
// TCP consumers (default 5s).
func WithWriteTimeout(d time.Duration) Option {
	return func(s *Server) { s.writeTimeout = d }
}

// New builds the HTTP handler tree over br.
func New(br *broker.Broker, opts ...Option) *Server {
	s := &Server{
		br:             br,
		heartbeat:      15 * time.Second,
		writeTimeout:   5 * time.Second,
		clientBodySize: 1 << 20,
	}
	for _, o := range opts {
		o(s)
	}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", s.handleHealth)
	mux.HandleFunc("POST /v1/events", s.handlePublish)
	mux.HandleFunc("GET /v1/events/stream", s.handleStream)
	mux.HandleFunc("GET /v1/events", s.handleReplay)
	mux.HandleFunc("GET /v1/stats", s.handleStats)
	s.mux = mux
	return s
}

// Handler exposes the routed http.Handler.
func (s *Server) Handler() http.Handler {
	return logRequests(s.mux)
}

func logRequests(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		rw := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(rw, r)
		log.Printf("%s %s %s -> %d (%s)", r.RemoteAddr, r.Method, r.URL.RequestURI(), rw.status, time.Since(start))
	})
}

type statusRecorder struct {
	http.ResponseWriter
	status int
}

func (r *statusRecorder) WriteHeader(code int) {
	r.status = code
	r.ResponseWriter.WriteHeader(code)
}

// Flush forwards to the wrapped ResponseWriter so SSE streaming works through
// the logging middleware.
func (r *statusRecorder) Flush() {
	if f, ok := r.ResponseWriter.(http.Flusher); ok {
		f.Flush()
	}
}

func writeError(w http.ResponseWriter, code int, msg string) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(map[string]string{"error": msg})
}

type publishRequest struct {
	Event string `json:"event"`
	Data  string `json:"data"`
}

type publishResponse struct {
	ID        int64     `json:"id"`
	Timestamp time.Time `json:"timestamp"`
}

func (s *Server) handlePublish(w http.ResponseWriter, r *http.Request) {
	var req publishRequest
	r.Body = http.MaxBytesReader(w, r.Body, s.clientBodySize)
	dec := json.NewDecoder(r.Body)
	if err := dec.Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
		return
	}
	if strings.TrimSpace(req.Data) == "" {
		writeError(w, http.StatusBadRequest, "field 'data' is required and must be non-empty")
		return
	}
	if err := sse.ValidateEventName(req.Event); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	e, err := s.br.Publish(req.Event, req.Data)
	if err != nil {
		if errors.Is(err, broker.ErrClosed) {
			writeError(w, http.StatusServiceUnavailable, "event store is closed")
			return
		}
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(http.StatusCreated)
	_ = json.NewEncoder(w).Encode(publishResponse{ID: e.ID, Timestamp: e.Timestamp})
}

func (s *Server) handleHealth(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	_ = json.NewEncoder(w).Encode(map[string]string{"status": "ok"})
}

func (s *Server) handleStats(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	_ = json.NewEncoder(w).Encode(s.br.Stats())
}

func (s *Server) handleReplay(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	after, err := parseCursor(q.Get("after"))
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	limit := 100
	if v := q.Get("limit"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n <= 0 || n > 10000 {
			writeError(w, http.StatusBadRequest, "limit must be in 1..10000")
			return
		}
		limit = n
	}
	events, oldest, last, ok, ahead := s.br.Replay(after)
	if !ok {
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		w.WriteHeader(http.StatusConflict)
		reason := sse.ResetCursorExpired
		if ahead {
			reason = sse.ResetCursorAhead
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"error":       "cursor not resumable",
			"reason":      reason,
			"oldest_id":   oldest,
			"last_id":     last,
			"resolution":  "perform a full resync; reopen the stream without Last-Event-ID",
			"received_id": after,
		})
		return
	}
	if len(events) > limit {
		events = events[:limit]
	}
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	_ = json.NewEncoder(w).Encode(map[string]any{
		"events":    events,
		"oldest_id": oldest,
		"last_id":   last,
		"has_more":  len(events) == limit,
	})
}

func parseCursor(v string) (int64, error) {
	if v == "" {
		return 0, nil
	}
	n, err := strconv.ParseInt(v, 10, 64)
	if err != nil || n < 0 {
		return 0, errors.New("cursor must be a non-negative integer event id (0 = from the oldest retained)")
	}
	return n, nil
}

// handleStream serves the SSE endpoint with replay-then-live resume.
func (s *Server) handleStream(w http.ResponseWriter, r *http.Request) {
	flusher, ok := w.(http.Flusher)
	if !ok {
		writeError(w, http.StatusInternalServerError, "streaming unsupported")
		return
	}

	// Cursor: explicit ?after= wins, else Last-Event-ID header.
	var rawCursor string
	if v := r.URL.Query().Get("after"); v != "" {
		rawCursor = v
	} else {
		rawCursor = r.Header.Get("Last-Event-ID")
	}
	cursor, err := parseCursor(rawCursor)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}

	h := w.Header()
	h.Set("Content-Type", "text/event-stream; charset=utf-8")
	h.Set("Cache-Control", "no-cache, no-transform")
	h.Set("Connection", "keep-alive")
	h.Set("X-Accel-Buffering", "no")
	w.WriteHeader(http.StatusOK)
	flusher.Flush()

	// Frames are written directly to the ResponseWriter and flushed one by
	// one: SSE is a stream of small, independently-delivered blocks, and a
	// buffering layer would make write backpressure invisible to the queue.
	rc := http.NewResponseController(w)
	df := &streamFlusher{w: w, rc: rc, timeout: s.writeTimeout, gate: s.gate}

	// 1) Register the live tail FIRST, capturing the head at subscription
	//    time. Live events can only have id > head; this is what makes the
	//    replay/live seam gap-free.
	sub, _ := s.br.Subscribe()
	defer s.br.Unsubscribe(sub)

	// 2) Validate the requested cursor against the retained window.
	if oldest, last, st := s.br.CheckCursor(cursor); st != broker.CursorOK {
		reason := sse.ResetCursorExpired
		if st == broker.CursorAhead {
			reason = sse.ResetCursorAhead
		}
		log.Printf("sse-resume: reset required cursor=%d oldest=%d last=%d reason=%s", cursor, oldest, last, reason)
		if err := sse.WriteReset(w, df, reason, oldest, last); err != nil {
			return
		}
		return // close immediately; client must resync
	}

	// 3) Replay everything strictly after the cursor. If the client
	//    reconnected with an id it had already received, that frame is
	//    delivered once more — clients dedupe by id.
	events, _, _, ok, _ := s.br.Replay(cursor)
	if !ok {
		// Race: retention could have advanced between CheckCursor and Replay
		// under heavy publishing; treat as a reset rather than a silent gap.
		oldest, last, st := s.br.CheckCursor(cursor)
		reason := sse.ResetCursorExpired
		if st == broker.CursorAhead {
			reason = sse.ResetCursorAhead
		}
		_ = sse.WriteReset(w, df, reason, oldest, last)
		return
	}

	ticker := time.NewTicker(s.heartbeat)
	defer ticker.Stop()

	// Announce the attached tail before the replay burst; the frame carries
	// no id line and therefore never affects the client's lastEventId.
	if err := sse.WriteNotice(w, df, "ready",
		"attached; replaying retained events after cursor, then live tail"); err != nil {
		return
	}
	for _, e := range events {
		if r.Context().Err() != nil {
			return
		}
		if err := sse.WriteFrame(w, df, e); err != nil {
			log.Printf("sse-resume: replay write failed at id=%d: %v", e.ID, err)
			return
		}
	}

	// 4) Live phase: heartbeats + fan-out. A channel close means this
	//    subscriber was evicted as a slow consumer; tell the client to
	//    reconnect (its events remain in the retained log).
	for {
		select {
		case <-r.Context().Done():
			return
		case <-ticker.C:
			if err := sse.WriteHeartbeat(w, df); err != nil {
				log.Printf("sse-resume: heartbeat failed: %v", err)
				return
			}
		case e, open := <-sub.C:
			if !open {
				_ = sse.WriteNotice(w, df, "slow_consumer",
					"server-side buffer overflowed; reconnect with Last-Event-ID to resume from the retained log")
				log.Printf("sse-resume: evicted subscriber closed at cursor~%d", lastSeenHint(cursor, events))
				return
			}
			if err := sse.WriteFrame(w, df, e); err != nil {
				log.Printf("sse-resume: live write failed at id=%d: %v", e.ID, err)
				return
			}
		}
	}
}

func lastSeenHint(cursor int64, replay []broker.Event) int64 {
	if n := len(replay); n > 0 {
		return replay[n-1].ID
	}
	return cursor
}

// streamFlusher enforces a per-flush write deadline so a stalled peer cannot
// pin a goroutine forever; the buffer overflow path handles slow readers that
// still acknowledge TCP.
type streamFlusher struct {
	w       io.Writer
	rc      *http.ResponseController
	timeout time.Duration
	gate    *flushGate
}

func (f *streamFlusher) Flush() error {
	// Tests use the gate to emulate a peer that stopped draining writes.
	if f.gate != nil {
		f.gate.wait()
	}
	if f.timeout > 0 {
		if err := f.rc.SetWriteDeadline(time.Now().Add(f.timeout)); err != nil {
			// Best effort: some listeners do not support deadlines.
			_ = err
		}
	}
	return f.rc.Flush()
}
