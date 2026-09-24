// Package server wires the broker and store to the net/http handlers.
package server

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"strconv"
	"strings"
	"time"

	"sseserver/internal/broker"
	"sseserver/internal/sse"
	"sseserver/internal/store"
)

// Config configures the HTTP layer.
type Config struct {
	Heartbeat  time.Duration
	MaxDataLen int // reject POST bodies larger than this (bytes)
}

// Server holds HTTP handlers.
type Server struct {
	broker *broker.Broker
	log    *store.Store
	cfg    Config
	logger *log.Logger
	mux    *http.ServeMux
}

// New builds the handler tree.
func New(b *broker.Broker, st *store.Store, cfg Config, logger *log.Logger) *Server {
	if cfg.Heartbeat <= 0 {
		cfg.Heartbeat = 15 * time.Second
	}
	if cfg.MaxDataLen <= 0 {
		cfg.MaxDataLen = 1 << 20
	}
	if logger == nil {
		logger = log.Default()
	}
	s := &Server{broker: b, log: st, cfg: cfg, logger: logger, mux: http.NewServeMux()}
	s.routes()
	return s
}

func (s *Server) routes() {
	s.mux.HandleFunc("GET /events", s.handleEvents)
	s.mux.HandleFunc("POST /events", s.handlePublish)
	s.mux.HandleFunc("GET /stats", s.handleStats)
	s.mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		_, _ = io.WriteString(w, "ok\n")
	})
}

// Handler exposes the routes.
func (s *Server) Handler() http.Handler { return s.mux }

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	_ = enc.Encode(v)
}

// handlePublish accepts {"data": "..."} or raw text/plain and stores an event.
func (s *Server) handlePublish(w http.ResponseWriter, r *http.Request) {
	r.Body = http.MaxBytesReader(w, r.Body, int64(s.cfg.MaxDataLen))

	var data string
	ct := r.Header.Get("Content-Type")
	switch {
	case strings.Contains(ct, "application/json"):
		var body struct {
			Data string `json:"data"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid JSON body: " + err.Error()})
			return
		}
		data = body.Data
	default:
		b, err := io.ReadAll(r.Body)
		if err != nil {
			var mbe *http.MaxBytesError
			if errors.As(err, &mbe) {
				writeJSON(w, http.StatusRequestEntityTooLarge, map[string]string{"error": "data too large"})
				return
			}
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
			return
		}
		data = string(b)
	}

	data = sse.NormalizeData(data)

	ev, err := s.broker.Publish(r.Context(), data)
	if err != nil {
		s.logger.Printf("publish failed: %v", err)
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "publish failed"})
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{
		"id":   ev.ID,
		"data": ev.Data,
		"ts":   ev.Time.Format(time.RFC3339Nano),
	})
}

// parseLastEventID reads the resume cursor. present is false when the
// header is absent (live-only subscription). Note http.Header.Set
// canonicalizes the key to "Last-Event-Id", so presence is checked via the
// canonicalized map, not a literal key.
func parseLastEventID(h http.Header) (id uint64, present bool, err error) {
	v := strings.TrimSpace(h.Get("Last-Event-ID"))
	if v == "" {
		return 0, false, nil
	}
	// Reject multi-valued headers rather than silently joining them.
	if vals := h.Values("Last-Event-ID"); len(vals) > 1 {
		return 0, false, errors.New("invalid Last-Event-ID: multiple header values")
	}
	// The SSE spec forbids U+0000 in IDs and the wire value is decimal here.
	if strings.ContainsRune(v, 0) {
		return 0, false, errors.New("invalid Last-Event-ID")
	}
	id, err = strconv.ParseUint(v, 10, 64)
	if err != nil {
		return 0, false, fmt.Errorf("invalid Last-Event-ID %q: %w", v, err)
	}
	return id, true, nil
}

func (s *Server) handleEvents(w http.ResponseWriter, r *http.Request) {
	lastID, hasCursor, err := parseLastEventID(r.Header)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}

	flusher, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "streaming unsupported", http.StatusInternalServerError)
		return
	}

	h := w.Header()
	h.Set("Content-Type", "text/event-stream")
	h.Set("Cache-Control", "no-cache")
	h.Set("Connection", "keep-alive")
	h.Set("X-Accel-Buffering", "no")
	w.WriteHeader(http.StatusOK)
	// Flush response headers immediately so clients receive 200 even before
	// the first event/heartbeat (otherwise an empty replay plus a long idle
	// interval stalls the HTTP response until the first byte).
	flusher.Flush()

	subscr := s.broker.Subscribe(lastID, hasCursor)
	if subscr.Reset != nil {
		// Cursor is outside the retained window: one explicit reset frame,
		// no id field, then close so the client can reconnect cleanly.
		payload, _ := json.Marshal(map[string]any{
			"reason": subscr.Reset.Reason,
			"oldest": subscr.Reset.Oldest,
			"last":   subscr.Reset.Last,
			"note":   "Last-Event-ID is outside the retained window; discard local state and reconnect with Last-Event-ID: <oldest-1>",
		})
		_ = sse.WriteControlEvent(w, "reset", string(payload))
		flusher.Flush()
		s.logger.Printf("stream rejected cursor=%d reason=%s oldest=%d last=%d",
			lastID, subscr.Reset.Reason, subscr.Reset.Oldest, subscr.Reset.Last)
		return
	}
	sub := subscr.Sub
	defer s.broker.Unsubscribe(sub)

	ctx := r.Context()
	heartbeat := time.NewTicker(s.cfg.Heartbeat)
	defer heartbeat.Stop()

	s.logger.Printf("stream open subscriber=%d cursor=%d replay=%d", sub.ID(), lastID, len(subscr.Replay))

	writeEvent := func(ev store.Event) bool {
		if err := sse.WriteEvent(w, ev.ID, "message", ev.Data); err != nil {
			return false
		}
		flusher.Flush()
		return true
	}

	if !subscr.LiveOnly {
		// Phase 1: drain the history snapshot to the network without holding
		// any broker lock. Events published meanwhile land in the
		// subscriber's pending list.
		for _, ev := range subscr.Replay {
			select {
			case <-ctx.Done():
				s.logger.Printf("stream closed during replay cursor=%d", lastID)
				return
			case <-sub.Done():
				s.writeSlowConsumer(w, flusher, lastID)
				return
			default:
			}
			if !writeEvent(ev) {
				return
			}
		}

		// Phase 2: hand over. Pending events must be emitted before channel
		// events to preserve strict ID order at the replay/live seam.
		pending, ok := s.broker.Activate(sub)
		if !ok {
			s.writeSlowConsumer(w, flusher, lastID)
			return
		}
		for _, ev := range pending {
			if !writeEvent(ev) {
				return
			}
		}
	}

	for {
		select {
		case <-ctx.Done():
			s.logger.Printf("stream closed by client cursor=%d", lastID)
			return

		case <-sub.Done():
			s.writeSlowConsumer(w, flusher, lastID)
			return

		case <-heartbeat.C:
			if _, err := fmt.Fprintf(w, ": heartbeat %s\n\n", time.Now().UTC().Format(time.RFC3339Nano)); err != nil {
				return
			}
			flusher.Flush()

		case ev := <-sub.Items():
			if !writeEvent(ev) {
				return
			}
		}
	}
}

// writeSlowConsumer emits one id-less error frame and returns; the deferred
// Unsubscribe removes the connection from the broker.
func (s *Server) writeSlowConsumer(w http.ResponseWriter, flusher http.Flusher, lastID uint64) {
	_ = sse.WriteControlEvent(w, "error", mapMustJSON(map[string]string{"reason": "slow_consumer"}))
	flusher.Flush()
	s.logger.Printf("stream force-closed: slow consumer cursor=%d", lastID)
}

func mapMustJSON(v any) string {
	b, err := json.Marshal(v)
	if err != nil {
		return "{}"
	}
	return string(b)
}

func (s *Server) handleStats(w http.ResponseWriter, r *http.Request) {
	bounds := s.log.Bounds()
	resp := map[string]any{
		"subscribers":     s.broker.SubscriberCount(),
		"retained_events": s.log.Count(),
		"slow_drops":      s.broker.SlowDrops(),
	}
	if bounds.Empty {
		resp["oldest_id"] = nil
		resp["last_id"] = nil
	} else {
		resp["oldest_id"] = bounds.Oldest
		resp["last_id"] = bounds.Last
	}
	writeJSON(w, http.StatusOK, resp)
}
