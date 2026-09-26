// Package server wires the HTTP endpoints to the shutdown coordinator.
// Liveness (/healthz) and readiness (/readyz) are deliberately separate:
// liveness stays 200 until process exit, readiness flips to 503 as soon as
// the receiving phase ends.
package server

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strconv"
	"time"

	"graceful-shutdown/internal/clock"
	"graceful-shutdown/internal/fakesvc"
	"graceful-shutdown/internal/ledger"
	"graceful-shutdown/internal/shutdown"
)

// StatusClientClosed is returned when an accepted request is cancelled by
// the shutdown coordinator's cancelling phase.
const StatusClientClosed = 499

// Server holds the handler dependencies.
type Server struct {
	coord *shutdown.Coordinator
	clk   clock.Clock
	db    *fakesvc.FakeDB
	queue *fakesvc.FakeQueue
	mux   *http.ServeMux
}

// New builds the HTTP handler graph.
func New(coord *shutdown.Coordinator, clk clock.Clock, db *fakesvc.FakeDB, q *fakesvc.FakeQueue) *Server {
	s := &Server{coord: coord, clk: clk, db: db, queue: q, mux: http.NewServeMux()}
	s.mux.HandleFunc("/healthz", s.handleHealthz)
	s.mux.HandleFunc("/readyz", s.handleReadyz)
	s.mux.HandleFunc("/work", s.handleWork)
	s.mux.HandleFunc("/stream", s.handleStream)
	s.mux.HandleFunc("/task", s.handleTask)
	s.mux.HandleFunc("/shutdown", s.handleShutdown)
	s.mux.HandleFunc("/state", s.handleState)
	return s
}

func (s *Server) Handler() http.Handler { return s.mux }

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

// accept gates a business endpoint through the coordinator and returns a
// context that is cancelled either by the shutdown cancel phase or by the
// client disconnecting.
func (s *Server) accept(w http.ResponseWriter, r *http.Request, kind string) (uint64, context.Context, bool) {
	id, rootCtx, ok := s.coord.TryAccept(r.URL.RequestURI(), kind)
	if !ok {
		writeJSON(w, http.StatusServiceUnavailable, map[string]any{
			"error": "server is shutting down; not accepting new work",
			"phase": s.coord.State().Phase,
		})
		return 0, nil, false
	}
	ctx, cancel := context.WithCancel(rootCtx)
	stop := context.AfterFunc(r.Context(), cancel)
	return id, &cleanupCtx{Context: ctx, stop: stop, cancel: cancel}, true
}

// cleanupCtx lets handlers release the AfterFunc hook when done.
type cleanupCtx struct {
	context.Context
	stop   func() bool
	cancel context.CancelFunc
}

func (c *cleanupCtx) cleanup() {
	c.stop()
	c.cancel()
}

func (s *Server) handleHealthz(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "alive"})
}

func (s *Server) handleReadyz(w http.ResponseWriter, r *http.Request) {
	if s.coord.Accepting() {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ready"})
		return
	}
	writeJSON(w, http.StatusServiceUnavailable, map[string]any{
		"status": "not-ready",
		"phase":  s.coord.State().Phase,
	})
}

// handleWork simulates a long request: a fake-DB query of ?ms= latency.
func (s *Server) handleWork(w http.ResponseWriter, r *http.Request) {
	id, ctx, ok := s.accept(w, r, "request")
	if !ok {
		return
	}
	cctx := ctx.(*cleanupCtx)
	defer cctx.cleanup()

	ms, _ := strconv.Atoi(r.URL.Query().Get("ms"))
	if ms <= 0 {
		ms = 100
	}
	query := r.URL.Query().Get("q")
	if query == "" {
		query = "default"
	}
	result, err := s.db.SlowQuery(ctx, query, time.Duration(ms)*time.Millisecond)
	if err != nil {
		s.coord.Finish(id, ledger.OutcomeCancelled, err.Error())
		writeJSON(w, StatusClientClosed, map[string]string{
			"outcome": string(ledger.OutcomeCancelled),
			"reason":  err.Error(),
		})
		return
	}
	s.coord.Finish(id, ledger.OutcomeCompleted, "")
	writeJSON(w, http.StatusOK, map[string]string{
		"outcome": string(ledger.OutcomeCompleted),
		"result":  result,
	})
}

// handleStream streams ?chunks= lines spaced ?interval_ms= apart. If the
// cancel phase interrupts it, a terminal "cancelled" line is written
// best-effort before returning.
func (s *Server) handleStream(w http.ResponseWriter, r *http.Request) {
	id, ctx, ok := s.accept(w, r, "request")
	if !ok {
		return
	}
	cctx := ctx.(*cleanupCtx)
	defer cctx.cleanup()

	chunks, _ := strconv.Atoi(r.URL.Query().Get("chunks"))
	if chunks <= 0 {
		chunks = 5
	}
	interval, _ := strconv.Atoi(r.URL.Query().Get("interval_ms"))
	if interval <= 0 {
		interval = 100
	}
	flusher, canFlush := w.(http.Flusher)
	w.Header().Set("Content-Type", "text/plain")
	w.WriteHeader(http.StatusOK)
	for i := 1; i <= chunks; i++ {
		select {
		case <-ctx.Done():
			fmt.Fprintf(w, "chunk %d/%d cancelled: %v\n", i, chunks, ctx.Err())
			if canFlush {
				flusher.Flush()
			}
			s.coord.Finish(id, ledger.OutcomeCancelled, ctx.Err().Error())
			return
		case <-s.clk.After(time.Duration(interval) * time.Millisecond):
		}
		fmt.Fprintf(w, "chunk %d/%d\n", i, chunks)
		if canFlush {
			flusher.Flush()
		}
	}
	s.coord.Finish(id, ledger.OutcomeCompleted, "")
}

// handleTask spawns a background task (a fake-queue publish). Once the
// receiving phase ends the coordinator rejects it: no new background tasks
// may be created after stop-accepting.
func (s *Server) handleTask(w http.ResponseWriter, r *http.Request) {
	id, ctx, ok := s.coord.TryAccept(r.URL.RequestURI(), "background-task")
	if !ok {
		writeJSON(w, http.StatusServiceUnavailable, map[string]any{
			"error": "server is shutting down; no new background tasks",
			"phase": s.coord.State().Phase,
		})
		return
	}
	msg := r.URL.Query().Get("msg")
	if msg == "" {
		msg = "background-job"
	}
	go func() {
		if err := s.queue.Publish(ctx, msg); err != nil {
			s.coord.Finish(id, ledger.OutcomeCancelled, err.Error())
			return
		}
		s.coord.Finish(id, ledger.OutcomeCompleted, "")
	}()
	writeJSON(w, http.StatusAccepted, map[string]any{
		"outcome": "accepted",
		"task_id": id,
	})
}

// handleShutdown triggers the four-phase shutdown. Repeated calls are
// idempotent and simply report the current state.
func (s *Server) handleShutdown(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "POST required"})
		return
	}
	s.coord.Shutdown()
	writeJSON(w, http.StatusAccepted, s.coord.State())
}

func (s *Server) handleState(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, s.coord.State())
}
