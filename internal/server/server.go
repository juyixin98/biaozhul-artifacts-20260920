// Package server exposes an hlc.Clock over HTTP using only net/http.
package server

import (
	"encoding/json"
	"net/http"

	"hlcservice/internal/hlc"
)

// Server wires an *hlc.Clock to HTTP handlers.
type Server struct {
	clock *hlc.Clock
	mux   *http.ServeMux
}

// New builds a server around the given clock.
func New(clock *hlc.Clock) *Server {
	s := &Server{clock: clock, mux: http.NewServeMux()}
	s.routes()
	return s
}

// Handler returns the root http.Handler.
func (s *Server) Handler() http.Handler { return s.mux }

func (s *Server) routes() {
	s.mux.HandleFunc("GET /{$}", s.handleIndex)
	s.mux.HandleFunc("GET /healthz", s.handleHealth)

	// Versioned, wire-stable API (see v1.go).
	s.mux.HandleFunc("POST /v1/tick", s.handleTick)
	s.mux.HandleFunc("GET /v1/now", s.handleNow)
	s.mux.HandleFunc("POST /v1/receive", s.handleReceive)
	s.mux.HandleFunc("GET /v1/status", s.handleStatus)

	// Convenience aliases at the root; same bodies as the /v1 endpoints.
	s.mux.HandleFunc("GET /timestamp", s.handleNow)
	s.mux.HandleFunc("POST /tick", s.handleTick)
	s.mux.HandleFunc("POST /remote", s.handleReceive)
}

type indexResponse struct {
	Node      string            `json:"node_id"`
	Endpoints map[string]string `json:"endpoints"`
}

func (s *Server) handleIndex(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, indexResponse{
		Node: s.clock.NodeID(),
		Endpoints: map[string]string{
			"GET  /healthz":    "liveness probe; includes physical_now_ms",
			"GET  /v1/now":     "current HLC timestamp without advancing it",
			"POST /v1/tick":    "stamp a local event and advance the clock",
			"POST /v1/receive": "receive a remote timestamp (object, wire string, or text)",
			"GET  /v1/status":  "operational counters and clock state",
			"GET  /timestamp":  "alias of GET /v1/now",
			"POST /tick":       "alias of POST /v1/tick",
			"POST /remote":     "alias of POST /v1/receive",
		},
	})
}

// healthResponse is returned by GET /healthz. physical_now_ms lets clients
// build near-future test timestamps without a separate time source.
type healthResponse struct {
	Status        string `json:"status"`
	NodeID        string `json:"node_id"`
	PhysicalNowMS int64  `json:"physical_now_ms"`
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	st := s.clock.Stats()
	writeJSON(w, http.StatusOK, healthResponse{
		Status:        "ok",
		NodeID:        s.clock.NodeID(),
		PhysicalNowMS: st.PhysicalNow,
	})
}

// envelope wraps every timestamp the service emits.
type envelope struct {
	NodeID    string        `json:"node_id"`
	Timestamp hlc.Timestamp `json:"timestamp"`
}

func (s *Server) envelope(t hlc.Timestamp) envelope {
	return envelope{NodeID: s.clock.NodeID(), Timestamp: t}
}

func (s *Server) handleTick(w http.ResponseWriter, r *http.Request) {
	t, err := s.clock.Tick()
	if err != nil {
		writeClockError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, s.envelope(t))
}

func (s *Server) handleNow(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, s.envelope(s.clock.Snapshot()))
}

func (s *Server) handleStatus(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, s.clock.Stats())
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

// statusRecorder captures the status code for request logging.
type statusRecorder struct {
	http.ResponseWriter
	status int
}

func (r *statusRecorder) WriteHeader(code int) {
	r.status = code
	r.ResponseWriter.WriteHeader(code)
}
