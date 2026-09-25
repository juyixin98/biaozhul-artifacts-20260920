// Package server implements the chunked result streaming HTTP service.
//
// Backpressure design: a per-request producer goroutine pulls items from
// the (fake) upstream into a bounded buffer. The buffer is bounded twice:
// by item slots (BufferSlots) and by total payload bytes (BufferBytes).
// When a slow client fills the buffer, the producer blocks, which stops
// it from pulling more items out of the upstream — backpressure
// propagates to the source. In-flight memory per request is bounded by
// (BufferSlots+2) items — full buffer, one item held by the blocked
// producer, one in the consumer handoff — and by BufferBytes plus one
// item being written.
package server

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"sync"
	"sync/atomic"
	"time"

	"streamback/internal/stream"
	"streamback/internal/upstream"
)

// Config tunes the streaming behavior of one Server.
type Config struct {
	// BufferSlots is the max number of items buffered between upstream
	// and the HTTP writer for one request.
	BufferSlots int
	// BufferBytes is the max total payload bytes buffered per request.
	BufferBytes int
	// MaxItemBytes rejects items larger than this before streaming
	// starts (HTTP 413).
	MaxItemBytes int
	// MaxLineBytes bounds a single protocol line accepted by decoders.
	MaxLineBytes int
}

// DefaultConfig returns a conservative default configuration.
func DefaultConfig() Config {
	return Config{
		BufferSlots:  4,
		BufferBytes:  256 * 1024,
		MaxItemBytes: 128 * 1024,
		MaxLineBytes: 1024 * 1024,
	}
}

// Validate checks the config for impossible combinations.
func (c Config) Validate() error {
	if c.BufferSlots < 1 {
		return errors.New("BufferSlots must be >= 1")
	}
	if c.MaxItemBytes < 1 {
		return errors.New("MaxItemBytes must be >= 1")
	}
	if c.BufferBytes < c.MaxItemBytes {
		return fmt.Errorf("BufferBytes (%d) must be >= MaxItemBytes (%d) so a single legal item always fits the buffer", c.BufferBytes, c.MaxItemBytes)
	}
	return nil
}

// Stats exposes observable memory/activity gauges for verification.
type Stats struct {
	// BufferedBytes is the current payload bytes sitting in per-request
	// buffers, summed across all in-flight requests.
	BufferedBytes atomic.Int64
	// HighWaterBytes is the maximum BufferedBytes ever observed.
	HighWaterBytes atomic.Int64
	// ActiveStreams is the number of streams currently being served.
	ActiveStreams atomic.Int64
}

// Snapshot is the JSON form of Stats.
type Snapshot struct {
	BufferedBytes  int64 `json:"bufferedBytes"`
	HighWaterBytes int64 `json:"highWaterBytes"`
	ActiveStreams  int64 `json:"activeStreams"`
}

// Server is the streaming HTTP service.
type Server struct {
	cfg  Config
	up   *upstream.Service
	mux  *http.ServeMux
	Stat Stats

	done      chan struct{}
	closeOnce sync.Once
}

// New builds a Server. up is the (fake) upstream service.
func New(cfg Config, up *upstream.Service) (*Server, error) {
	if err := cfg.Validate(); err != nil {
		return nil, err
	}
	s := &Server{
		cfg:  cfg,
		up:   up,
		mux:  http.NewServeMux(),
		done: make(chan struct{}),
	}
	s.mux.HandleFunc("/stream", s.handleStream)
	s.mux.HandleFunc("/stats", s.handleStats)
	return s, nil
}

// Handler returns the root HTTP handler.
func (s *Server) Handler() http.Handler { return s.mux }

// Shutdown signals in-flight streams to finish with a SERVER_SHUTDOWN
// trailer and waits for them to return, up to grace.
func (s *Server) Shutdown(grace time.Duration) error {
	s.closeOnce.Do(func() { close(s.done) })

	deadline := time.Now().Add(grace)
	for s.Stat.ActiveStreams.Load() != 0 {
		if time.Now().After(deadline) {
			return errors.New("shutdown: grace period expired with streams still active")
		}
		time.Sleep(time.Millisecond)
	}
	return nil
}

func (s *Server) handleStats(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(Snapshot{
		BufferedBytes:  s.Stat.BufferedBytes.Load(),
		HighWaterBytes: s.Stat.HighWaterBytes.Load(),
		ActiveStreams:  s.Stat.ActiveStreams.Load(),
	})
}

// parseSpec reads the fault-injection parameters from the query string.
func parseSpec(r *http.Request) (upstream.Spec, error) {
	q := r.URL.Query()
	get := func(key string, def int64) (int64, error) {
		v := q.Get(key)
		if v == "" {
			return def, nil
		}
		n, err := strconv.ParseInt(v, 10, 64)
		if err != nil {
			return 0, fmt.Errorf("parameter %q: %w", key, err)
		}
		return n, nil
	}

	count, err := get("count", 10)
	if err != nil {
		return upstream.Spec{}, err
	}
	itemBytes, err := get("itemBytes", 1024)
	if err != nil {
		return upstream.Spec{}, err
	}
	itemDelay, err := get("itemDelayMs", 0)
	if err != nil {
		return upstream.Spec{}, err
	}
	failAt, err := get("failAt", 0)
	if err != nil {
		return upstream.Spec{}, err
	}
	spec := upstream.Spec{
		Count:     int(count),
		ItemBytes: int(itemBytes),
		ItemDelay: itemDelay,
		FailAt:    int(failAt),
		FailMsg:   q.Get("failMsg"),
	}
	if err := spec.Validate(); err != nil {
		return upstream.Spec{}, err
	}
	return spec, nil
}

func writeJSONError(w http.ResponseWriter, status int, code, msg string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]string{
		"error":   code,
		"message": msg,
	})
}

// event is one unit of work flowing from producer to writer.
type event struct {
	item *upstream.Item
	err  error
	done bool
	size int // payload bytes accounted in the byte budget; 0 for non-item events
}

// byteBudget is a counting semaphore over buffered payload bytes.
// Operations are O(1): accounting must never cost more than the I/O it
// gates, or the bookkeeping itself becomes the bottleneck.
type byteBudget struct {
	mu   sync.Mutex
	used int
	cap  int
	// ch is closed to broadcast a release to all waiters, then replaced.
	ch chan struct{}
}

func newByteBudget(capacity int) *byteBudget {
	return &byteBudget{cap: capacity, ch: make(chan struct{})}
}

// acquire blocks until n units are free or ctx is done. n must be <= capacity.
func (b *byteBudget) acquire(ctx context.Context, n int) bool {
	b.mu.Lock()
	for b.used+n > b.cap {
		wait := b.ch
		b.mu.Unlock()
		select {
		case <-ctx.Done():
			return false
		case <-wait:
		}
		b.mu.Lock()
	}
	b.used += n
	b.mu.Unlock()
	return true
}

func (b *byteBudget) release(n int) {
	b.mu.Lock()
	b.used -= n
	close(b.ch)
	b.ch = make(chan struct{})
	b.mu.Unlock()
}

func (s *Server) handleStream(w http.ResponseWriter, r *http.Request) {
	spec, err := parseSpec(r)
	if err != nil {
		writeJSONError(w, http.StatusBadRequest, "BAD_REQUEST", err.Error())
		return
	}
	// Explicit rejection of oversized single items, before any streaming.
	if spec.ItemBytes > s.cfg.MaxItemBytes {
		writeJSONError(w, http.StatusRequestEntityTooLarge, stream.CodeItemTooLarge,
			fmt.Sprintf("itemBytes %d exceeds max %d", spec.ItemBytes, s.cfg.MaxItemBytes))
		return
	}

	flusher, ok := w.(http.Flusher)
	if !ok {
		writeJSONError(w, http.StatusInternalServerError, "NO_STREAMING", "response writer cannot flush")
		return
	}

	s.Stat.ActiveStreams.Add(1)
	defer s.Stat.ActiveStreams.Add(-1)

	// reqCtx ends when the client disconnects OR the server shuts down.
	reqCtx, cancel := context.WithCancel(r.Context())
	go func() {
		select {
		case <-s.done:
			cancel()
		case <-reqCtx.Done():
		}
	}()

	w.Header().Set("Content-Type", "application/x-ndjson")
	w.Header().Set("X-Accel-Buffering", "no")
	w.Header().Set("Cache-Control", "no-cache")
	w.WriteHeader(http.StatusOK)
	flusher.Flush()

	events := make(chan event, s.cfg.BufferSlots)
	budget := newByteBudget(s.cfg.BufferBytes)
	prodDone := make(chan struct{})
	go func() {
		s.produce(reqCtx, spec, events, budget)
		close(prodDone)
	}()

	// On exit, stop the producer and drain whatever it already buffered so
	// the byte budget and the BufferedBytes gauge return to zero. After
	// prodDone is closed the producer can no longer send, so a
	// non-blocking drain is complete.
	defer func() {
		cancel()
		<-prodDone
		for {
			select {
			case ev := <-events:
				if ev.size > 0 {
					s.addBuffered(-int64(ev.size))
					budget.release(ev.size)
				}
			default:
				return
			}
		}
	}()

	sent := 0
	writeTrailer := func(rec stream.Record) {
		_ = stream.Encode(w, rec)
		flusher.Flush()
	}

	for {
		select {
		case <-reqCtx.Done():
			// Client disconnect or server shutdown. If the connection is
			// still usable (shutdown case) the trailer reaches the client;
			// if the client is gone the write simply fails and is ignored.
			select {
			case <-s.done:
				writeTrailer(stream.Error(stream.CodeServerShutdown, "server is shutting down", sent))
			default:
			}
			return
		case ev := <-events:
			switch {
			case ev.err != nil:
				writeTrailer(stream.Error(stream.CodeUpstreamFailure, ev.err.Error(), sent))
				return
			case ev.done:
				writeTrailer(stream.End(sent))
				return
			default:
				s.addBuffered(-int64(ev.size)) // dequeued from the buffer
				if err := stream.Encode(w, stream.Item(ev.item.Seq, ev.item.Payload)); err != nil {
					// Client went away mid-write; nothing more to do.
					return
				}
				flusher.Flush()
				sent++
				budget.release(ev.size)
			}
		}
	}
}

// produce pulls items from the upstream and forwards them into the
// bounded events channel, acquiring byte budget first. It blocks when
// the buffer is full — that block is the backpressure mechanism.
func (s *Server) produce(ctx context.Context, spec upstream.Spec, events chan<- event, budget *byteBudget) {
	items, errs := s.up.Stream(ctx, spec)
	for items != nil || errs != nil {
		select {
		case <-ctx.Done():
			return
		case it, ok := <-items:
			if !ok {
				items = nil
				continue
			}
			size := len(it.Payload)
			if !budget.acquire(ctx, size) {
				return
			}
			// BufferedBytes counts bytes produced but not yet dequeued by
			// the writer: items in the channel plus the one item this
			// producer may be holding while blocked on a full buffer.
			// A consumer that received an item but has not decremented
			// yet adds a transient third tier, so the exact bound is
			// BufferSlots+2 items (and always BufferBytes, since every
			// counted item holds budget tokens).
			s.addBuffered(int64(size))
			select {
			case events <- event{item: &it, size: size}:
			case <-ctx.Done():
				s.addBuffered(-int64(size))
				budget.release(size)
				return
			}
		case err, ok := <-errs:
			if !ok {
				errs = nil
				continue
			}
			select {
			case events <- event{err: err}:
			case <-ctx.Done():
			}
			return
		}
	}
	// Upstream finished cleanly.
	select {
	case events <- event{done: true}:
	case <-ctx.Done():
	}
}

func (s *Server) addBuffered(delta int64) {
	cur := s.Stat.BufferedBytes.Add(delta)
	for {
		hi := s.Stat.HighWaterBytes.Load()
		if cur <= hi || s.Stat.HighWaterBytes.CompareAndSwap(hi, cur) {
			return
		}
	}
}
