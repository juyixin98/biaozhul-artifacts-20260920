// Package server exposes the chunked NDJSON streaming endpoint. Slow
// clients fill a bounded per-connection buffer which propagates backpressure
// to the in-process fake upstream; oversized items are rejected explicitly;
// failures after partial delivery are reported as protocol trailer records.
package server

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"strconv"
	"sync"
	"sync/atomic"
	"time"

	"streambp/internal/clock"
	"streambp/internal/protocol"
	"streambp/internal/stream"
	"streambp/internal/upstream"
)

// Config tunes the streaming server.
type Config struct {
	// BufferCapacity is the per-connection bounded buffer size (items).
	BufferCapacity int
	// MaxItemBytes rejects any single item larger than this.
	MaxItemBytes int
	// MaxBufferedBytes bounds total buffer memory across all connections.
	MaxBufferedBytes int64
	// MaxItemCount caps the items a single request may ask for.
	MaxItemCount int
	// Defaults is the baseline upstream behaviour; query params override it.
	Defaults upstream.Config
}

func (c *Config) setDefaults() {
	if c.BufferCapacity <= 0 {
		c.BufferCapacity = 8
	}
	if c.MaxItemBytes <= 0 {
		c.MaxItemBytes = 1 << 20
	}
	if c.MaxBufferedBytes <= 0 {
		c.MaxBufferedBytes = 16 << 20
	}
	if c.MaxItemCount <= 0 {
		c.MaxItemCount = 100000
	}
	if c.Defaults.ItemCount <= 0 {
		c.Defaults.ItemCount = 100
	}
	if c.Defaults.ItemSize <= 0 {
		c.Defaults.ItemSize = 256
	}
	if c.Defaults.FailAfter == 0 {
		c.Defaults.FailAfter = -1
	}
}

func (c Config) validate() error {
	if c.BufferCapacity <= 0 {
		return errors.New("server: BufferCapacity must be positive")
	}
	if c.MaxItemBytes <= 0 {
		return errors.New("server: MaxItemBytes must be positive")
	}
	if int64(c.MaxItemBytes) > c.MaxBufferedBytes {
		return fmt.Errorf("server: MaxBufferedBytes (%d) must be >= MaxItemBytes (%d)",
			c.MaxBufferedBytes, c.MaxItemBytes)
	}
	return nil
}

// Stats is the structured observability snapshot served at /stats.
type Stats struct {
	ActiveConnections int   `json:"activeConnections"`
	BufferedBytes     int64 `json:"bufferedBytes"`
	MaxBufferedBytes  int64 `json:"maxBufferedBytes"`
	BufferCapacity    int   `json:"bufferCapacity"`
	MaxItemBytes      int   `json:"maxItemBytes"`
	ProducedTotal     int64 `json:"producedTotal"`
	SentTotal         int64 `json:"sentTotal"`
}

// Server streams upstream items as NDJSON with bounded memory.
type Server struct {
	cfg Config
	clk clock.Clock
	lim *stream.Limiter
	mux *http.ServeMux

	done      chan struct{}
	closeOnce sync.Once
	mu        sync.Mutex // guards closed vs wg.Add
	closed    bool
	wg        sync.WaitGroup

	activeConns   atomic.Int64
	producedTotal atomic.Int64
	sentTotal     atomic.Int64
}

func New(cfg Config, clk clock.Clock) (*Server, error) {
	cfg.setDefaults()
	if err := cfg.validate(); err != nil {
		return nil, err
	}
	s := &Server{
		cfg:  cfg,
		clk:  clk,
		lim:  stream.NewLimiter(cfg.MaxBufferedBytes),
		done: make(chan struct{}),
	}
	s.mux = http.NewServeMux()
	s.mux.HandleFunc("/stream", s.handleStream)
	s.mux.HandleFunc("/stats", s.handleStats)
	s.mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok\n"))
	})
	return s, nil
}

// Handler exposes the server's routes (useful with httptest).
func (s *Server) Handler() http.Handler { return s.mux }

// Stats returns a consistent-enough observability snapshot.
func (s *Server) Stats() Stats {
	return Stats{
		ActiveConnections: int(s.activeConns.Load()),
		BufferedBytes:     s.lim.Current(),
		MaxBufferedBytes:  s.lim.Max(),
		BufferCapacity:    s.cfg.BufferCapacity,
		MaxItemBytes:      s.cfg.MaxItemBytes,
		ProducedTotal:     s.producedTotal.Load(),
		SentTotal:         s.sentTotal.Load(),
	}
}

// Close signals in-flight handlers to stop and waits for them.
func (s *Server) Close() {
	s.closeOnce.Do(func() {
		s.mu.Lock()
		s.closed = true
		close(s.done)
		s.mu.Unlock()
	})
	s.wg.Wait()
}

// ListenAndServe runs until ctx is cancelled, then shuts down gracefully:
// it stops accepting, signals in-flight streams, and waits for them.
func (s *Server) ListenAndServe(ctx context.Context, addr string) error {
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		return fmt.Errorf("server: listen %s: %w", addr, err)
	}
	hs := &http.Server{Handler: s.mux}
	errCh := make(chan error, 1)
	go func() { errCh <- hs.Serve(ln) }()
	select {
	case err := <-errCh:
		return err
	case <-ctx.Done():
	}
	s.Close()
	shutCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := hs.Shutdown(shutCtx); err != nil {
		_ = hs.Close()
		return fmt.Errorf("server: shutdown: %w", err)
	}
	return nil
}

func (s *Server) handleStats(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(s.Stats())
}

func (s *Server) handleStream(w http.ResponseWriter, r *http.Request) {
	ucfg, err := s.upstreamConfig(r)
	if err != nil {
		writeJSONError(w, http.StatusRequestEntityTooLarge, "item_too_large", err.Error())
		return
	}
	src := upstream.NewFake(ucfg, s.clk)
	s.serveStream(w, r, src)
}

// upstreamConfig overlays query params on the configured defaults.
func (s *Server) upstreamConfig(r *http.Request) (upstream.Config, error) {
	cfg := s.cfg.Defaults
	q := r.URL.Query()
	if v := q.Get("items"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n < 0 || n > s.cfg.MaxItemCount {
			return cfg, fmt.Errorf("items must be in [0,%d]", s.cfg.MaxItemCount)
		}
		cfg.ItemCount = n
	}
	if v := q.Get("item_size"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n <= 0 {
			return cfg, fmt.Errorf("item_size must be positive")
		}
		if n > s.cfg.MaxItemBytes {
			return cfg, fmt.Errorf("item_size %d exceeds max_item_bytes %d", n, s.cfg.MaxItemBytes)
		}
		cfg.ItemSize = n
	}
	if v := q.Get("item_delay_ms"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n < 0 {
			return cfg, fmt.Errorf("item_delay_ms must be >= 0")
		}
		cfg.Delay = time.Duration(n) * time.Millisecond
	}
	if v := q.Get("fail_after"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil {
			return cfg, fmt.Errorf("fail_after must be an integer")
		}
		cfg.FailAfter = n
	}
	return cfg, nil
}

// countingSource wraps a source to count produced items for /stats.
type countingSource struct {
	inner stream.Source
	count *atomic.Int64
}

func (c countingSource) Next(ctx context.Context) ([]byte, error) {
	b, err := c.inner.Next(ctx)
	if err == nil {
		c.count.Add(1)
	}
	return b, err
}

// serveStream is the core pipeline: bounded buffer between upstream and the
// response writer, explicit size rejection, trailer records for late errors.
func (s *Server) serveStream(w http.ResponseWriter, r *http.Request, src stream.Source) {
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		writeJSONError(w, http.StatusServiceUnavailable, "shutting_down", "server is shutting down")
		return
	}
	s.wg.Add(1)
	s.mu.Unlock()
	defer s.wg.Done()
	s.activeConns.Add(1)
	defer s.activeConns.Add(-1)

	flusher, ok := w.(http.Flusher)
	if !ok {
		writeJSONError(w, http.StatusInternalServerError, "no_streaming", "response writer cannot flush")
		return
	}

	// Cancel production on client disconnect OR server shutdown.
	ctx, cancel := context.WithCancel(r.Context())
	defer cancel()
	go func() {
		select {
		case <-s.done:
			cancel()
		case <-ctx.Done():
		}
	}()

	counted := countingSource{inner: src, count: &s.producedTotal}
	results := stream.Produce(ctx, counted, s.cfg.BufferCapacity, s.lim)

	w.Header().Set("Content-Type", "application/x-ndjson")
	w.Header().Set("X-Accel-Buffering", "no")
	w.WriteHeader(http.StatusOK)
	flusher.Flush()

	sent := 0
	for res := range results {
		if res.Err != nil {
			s.writeTrailer(w, flusher, protocol.Error(
				protocol.CodeUpstreamError, res.Err.Error(), sent))
			stream.Drain(results, s.lim)
			return
		}
		n := len(res.Data)
		if n > s.cfg.MaxItemBytes {
			s.lim.Release(int64(n))
			s.writeTrailer(w, flusher, protocol.Error(
				protocol.CodeItemTooLarge,
				fmt.Sprintf("item of %d bytes exceeds max_item_bytes %d", n, s.cfg.MaxItemBytes),
				sent))
			stream.Drain(results, s.lim)
			return
		}
		if err := protocol.Encode(w, protocol.Data(sent+1, res.Data)); err != nil {
			// Client disconnected mid-stream: reclaim buffer and stop.
			s.lim.Release(int64(n))
			stream.Drain(results, s.lim)
			return
		}
		s.lim.Release(int64(n))
		sent++
		s.sentTotal.Add(1)
		flusher.Flush()
		if ctx.Err() != nil {
			stream.Drain(results, s.lim)
			return
		}
	}
	// Producer finished. Distinguish clean EOF from cancellation/shutdown.
	if ctx.Err() != nil {
		s.writeTrailer(w, flusher, protocol.Error(
			protocol.CodeShuttingDown, "stream interrupted before completion", sent))
		return
	}
	s.writeTrailer(w, flusher, protocol.End(sent))
}

func (s *Server) writeTrailer(w http.ResponseWriter, f http.Flusher, r protocol.Record) {
	if err := protocol.Encode(w, r); err != nil {
		return // client already gone; nothing more to do
	}
	f.Flush()
}

func writeJSONError(w http.ResponseWriter, status int, code, msg string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]string{"error": code, "message": msg})
}
