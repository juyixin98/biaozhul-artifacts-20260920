// Package httpapi exposes a scheduler over a small local HTTP JSON API.
package httpapi

import (
	"encoding/json"
	"errors"
	"net/http"

	"agingqueue/queue"
)

// Server wraps a *queue.Scheduler with HTTP handlers.
type Server struct {
	sched *queue.Scheduler
	mux   *http.ServeMux
}

// NewServer builds the handler tree. The returned *Server implements
// http.Handler; the same scheduler stays usable directly from Go code.
func NewServer(s *queue.Scheduler) *Server {
	srv := &Server{sched: s, mux: http.NewServeMux()}
	srv.routes()
	return srv
}

func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	s.mux.ServeHTTP(w, r)
}

func (s *Server) routes() {
	s.mux.HandleFunc("GET /health", s.health)
	s.mux.HandleFunc("POST /jobs", s.submit)
	s.mux.HandleFunc("GET /jobs", s.list)
	s.mux.HandleFunc("GET /jobs/{id}", s.get)
	s.mux.HandleFunc("POST /jobs/{id}/cancel", s.cancel)
	s.mux.HandleFunc("GET /stats", s.stats)
	s.mux.HandleFunc("GET /events", s.events)
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, status int, code, msg string) {
	writeJSON(w, status, map[string]string{"error": code, "message": msg})
}

func (s *Server) health(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

// submitRequest is the wire form of queue.Submit; durations are strings
// such as "500ms".
type submitRequest struct {
	Type        string          `json:"type"`
	Payload     json.RawMessage `json:"payload,omitempty"`
	Priority    int             `json:"priority"`
	MaxPriority int             `json:"maxPriority,omitempty"`
	MaxAttempts int             `json:"maxAttempts,omitempty"`
	Delay       queue.Duration  `json:"delay,omitempty"`
	ID          string          `json:"id,omitempty"`
}

func (s *Server) submit(w http.ResponseWriter, r *http.Request) {
	var req submitRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, "bad_json", err.Error())
		return
	}
	j, err := s.sched.Submit(queue.Submit{
		Type:        req.Type,
		Payload:     req.Payload,
		Priority:    req.Priority,
		MaxPriority: req.MaxPriority,
		MaxAttempts: req.MaxAttempts,
		Delay:       req.Delay,
		ID:          req.ID,
	})
	if err != nil {
		status := http.StatusInternalServerError
		switch {
		case errors.Is(err, queue.ErrNoType), errors.Is(err, queue.ErrBadConfig):
			status = http.StatusBadRequest
		}
		writeErr(w, status, "submit_failed", err.Error())
		return
	}
	writeJSON(w, http.StatusCreated, j)
}

func (s *Server) list(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"jobs": s.sched.List()})
}

func (s *Server) get(w http.ResponseWriter, r *http.Request) {
	j, err := s.sched.Get(r.PathValue("id"))
	if err != nil {
		writeErr(w, http.StatusNotFound, "not_found", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, j)
}

func (s *Server) cancel(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	err := s.sched.Cancel(id)
	switch {
	case err == nil:
		j, _ := s.sched.Get(id)
		writeJSON(w, http.StatusOK, j)
	case errors.Is(err, queue.ErrNotFound):
		writeErr(w, http.StatusNotFound, "not_found", err.Error())
	case errors.Is(err, queue.ErrTerminal):
		writeErr(w, http.StatusConflict, "already_terminal", err.Error())
	default:
		writeErr(w, http.StatusInternalServerError, "cancel_failed", err.Error())
	}
}

func (s *Server) stats(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, s.sched.Stats())
}

// events returns recent structured events from the in-memory sink. With
// ?stream=1 it upgrades to Server-Sent Events and streams live events until
// the client disconnects; otherwise it snapshots and returns.
func (s *Server) events(w http.ResponseWriter, r *http.Request) {
	ms, ok := s.sched.Sink().(*queue.MemorySink)
	if !ok {
		writeErr(w, http.StatusNotImplemented, "sink_not_inspectable",
			"the configured event sink is not a MemorySink")
		return
	}
	if r.URL.Query().Get("stream") != "1" {
		writeJSON(w, http.StatusOK, map[string]any{"events": ms.Events()})
		return
	}

	flusher, ok := w.(http.Flusher)
	if !ok {
		writeErr(w, http.StatusInternalServerError, "no_streaming", "response writer cannot stream")
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	w.WriteHeader(http.StatusOK)

	// Stream via a short-polling bridge: MemorySink has no subscribe API,
	// and a deterministic poll keeps the bridge trivial to reason about.
	seen := ms.Len()
	ctx := r.Context()
	for {
		evs := ms.Events()
		for ; seen < len(evs); seen++ {
			b, _ := json.Marshal(evs[seen])
			_, _ = w.Write([]byte("data: "))
			_, _ = w.Write(b)
			_, _ = w.Write([]byte("\n\n"))
		}
		flusher.Flush()
		select {
		case <-ctx.Done():
			return
		case <-pollAfter():
		}
	}
}
