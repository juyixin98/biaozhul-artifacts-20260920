// Package fakesvc implements an in-process HTTP service that stands in for
// an external dependency. It never leaves the machine: requests can block on
// a named gate (released by test or admin call), block for a fixed hold, or
// observe client cancellation. Connection and in-flight counters let tests
// prove that cancellation drains work and frees connections.
package fakesvc

import (
	"context"
	"encoding/json"
	"net"
	"net/http"
	"strconv"
	"sync"
	"sync/atomic"
	"time"
)

// Stats is a point-in-time snapshot of service instrumentation.
type Stats struct {
	Name        string `json:"name"`
	Total       int64  `json:"total_requests"`
	Completed   int64  `json:"completed"`
	Failed      int64  `json:"failed"`
	Canceled    int64  `json:"canceled_clients"`
	InFlight    int64  `json:"in_flight"`
	ActiveConns int64  `json:"active_connections"`
	OpenGates   int    `json:"open_gates"`
}

// Server is a controllable fake downstream service.
type Server struct {
	Name string

	srv  *http.Server
	ln   net.Listener
	addr string

	total     atomic.Int64
	completed atomic.Int64
	failed    atomic.Int64
	canceled  atomic.Int64
	inFlight  atomic.Int64
	active    atomic.Int64

	gatesMu sync.Mutex
	gates   map[string]chan struct{}
}

// New creates but does not start a service. Call Start.
func New(name string) *Server {
	s := &Server{Name: name, gates: make(map[string]chan struct{})}
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", s.handleHealth)
	mux.HandleFunc("/work", s.handleWork)
	mux.HandleFunc("/stats", s.handleStats)
	mux.HandleFunc("/admin/release", s.handleRelease)
	s.srv = &http.Server{
		Handler:      mux,
		ReadTimeout:  5 * time.Second,
		WriteTimeout: 0, // handlers may block for as long as the client waits
		ConnState:    s.connState,
	}
	return s
}

// Start binds the service to an ephemeral loopback port and serves.
func (s *Server) Start() error {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return err
	}
	s.ln = ln
	s.addr = ln.Addr().String()
	go func() { _ = s.srv.Serve(ln) }()
	return nil
}

// URL returns the base URL of the running service.
func (s *Server) URL() string { return "http://" + s.addr }

// Shutdown gracefully stops the service, waiting for in-flight requests.
func (s *Server) Shutdown(ctx context.Context) error {
	return s.srv.Shutdown(ctx)
}

// Release opens the named gate, unblocking every request waiting on it.
// It is idempotent: releasing an unknown or already open gate is a no-op.
func (s *Server) Release(gate string) {
	s.gatesMu.Lock()
	ch, ok := s.gates[gate]
	if ok {
		delete(s.gates, gate)
	}
	s.gatesMu.Unlock()
	if ok {
		close(ch)
	}
}

// Snapshot returns the current counters.
func (s *Server) Snapshot() Stats {
	s.gatesMu.Lock()
	gates := len(s.gates)
	s.gatesMu.Unlock()
	return Stats{
		Name:        s.Name,
		Total:       s.total.Load(),
		Completed:   s.completed.Load(),
		Failed:      s.failed.Load(),
		Canceled:    s.canceled.Load(),
		InFlight:    s.inFlight.Load(),
		ActiveConns: s.active.Load(),
		OpenGates:   gates,
	}
}

func (s *Server) connState(_ net.Conn, state http.ConnState) {
	switch state {
	case http.StateNew:
		s.active.Add(1)
	case http.StateClosed:
		s.active.Add(-1)
	}
}

// gateChan returns the gate, creating a closed-wait channel on first use.
func (s *Server) gateChan(name string) chan struct{} {
	s.gatesMu.Lock()
	defer s.gatesMu.Unlock()
	ch, ok := s.gates[name]
	if !ok {
		ch = make(chan struct{})
		s.gates[name] = ch
	}
	return ch
}

// unregisterGate removes the gate entry only while it still maps to this
// handler's channel; a later request reusing the same name must not have its
// fresh gate deleted.
func (s *Server) unregisterGate(name string, ch chan struct{}) {
	s.gatesMu.Lock()
	defer s.gatesMu.Unlock()
	if cur, ok := s.gates[name]; ok && cur == ch {
		delete(s.gates, name)
	}
}

type workResponse struct {
	Service string `json:"service"`
	Gate    string `json:"gate,omitempty"`
	HeldMS  int64  `json:"held_ms,omitempty"`
}

func (s *Server) handleHealth(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	_, _ = w.Write([]byte(`{"status":"ok"}`))
}

func (s *Server) handleWork(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	gate := q.Get("gate")
	var hold time.Duration
	if ms, err := strconv.ParseInt(q.Get("hold_ms"), 10, 64); err == nil && ms > 0 {
		hold = time.Duration(ms) * time.Millisecond
	}

	// Register the gate BEFORE marking the request in flight, so an
	// observer waiting on InFlight==1 is guaranteed the gate exists and a
	// subsequent Release cannot be lost.
	var gateCh chan struct{}
	if gate != "" {
		gateCh = s.gateChan(gate)
		defer s.unregisterGate(gate, gateCh)
	}

	s.total.Add(1)
	s.inFlight.Add(1)
	defer s.inFlight.Add(-1)

	var holdCh <-chan time.Time
	if hold > 0 {
		t := time.NewTimer(hold)
		defer t.Stop()
		holdCh = t.C
	}

	// Only wait when there is something to wait on; otherwise proceed
	// immediately. A nil case channel never fires, so a select containing
	// only nil channels plus ctx.Done would block until cancel.
	if gateCh != nil || holdCh != nil {
		select {
		case <-r.Context().Done():
			// The client went away (or its parent request was canceled).
			// There is no point writing a response; record the cancellation.
			s.canceled.Add(1)
			return
		case <-gateCh:
		case <-holdCh:
		}
	}

	if r.Header.Get("X-Fail") == "1" {
		s.failed.Add(1)
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write([]byte(`{"error":"injected downstream failure"}`))
		return
	}

	s.completed.Add(1)
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(workResponse{
		Service: s.Name,
		Gate:    gate,
		HeldMS:  hold.Milliseconds(),
	})
}

func (s *Server) handleStats(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(s.Snapshot())
}

func (s *Server) handleRelease(w http.ResponseWriter, r *http.Request) {
	gate := r.URL.Query().Get("gate")
	if gate == "" {
		http.Error(w, `{"error":"gate required"}`, http.StatusBadRequest)
		return
	}
	s.Release(gate)
	w.Header().Set("Content-Type", "application/json")
	_, _ = w.Write([]byte(`{"released":"` + gate + `"}`))
}
