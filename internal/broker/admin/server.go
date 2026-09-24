// Package admin exposes the small operational HTTP surface used by the
// automated acceptance tests and manual verification:
//
//	GET  /healthz                 liveness
//	GET  /metrics                 consumer counters
//	GET  /state                   current device business state (online window)
//	POST /faults/arm?device=ID    force business tx rollback for a device
//	POST /faults/dropack?device=ID  one-shot: commit succeeds, ACK is lost
//	POST /faults/clear?device=ID  stop forcing ("*" clears all)
//	GET  /faults                  list armed devices
//
// No frontend: JSON only.
package admin

import (
	"context"
	"encoding/json"
	"net/http"
	"time"

	"mqttredel/internal/broker"
	"mqttredel/internal/store"
)

// Server bundles the admin HTTP handlers.
type Server struct {
	consumer *broker.Consumer
	faults   *broker.Faults
	online   time.Duration
	srv      *http.Server
}

// New constructs the admin server (not started yet).
func New(addr string, c *broker.Consumer, f *broker.Faults, onlineWindow time.Duration) *Server {
	s := &Server{consumer: c, faults: f, online: onlineWindow}
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", s.healthz)
	mux.HandleFunc("/metrics", s.metrics)
	mux.HandleFunc("/state", s.state)
	mux.HandleFunc("/faults", s.faultsHandler)
	mux.HandleFunc("/faults/arm", s.arm)
	mux.HandleFunc("/faults/dropack", s.dropAck)
	mux.HandleFunc("/faults/clear", s.clear)
	s.srv = &http.Server{Addr: addr, Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	return s
}

// Start serves in the background.
func (s *Server) Start() error {
	go func() { _ = s.srv.ListenAndServe() }()
	return nil
}

// Close stops the server.
func (s *Server) Close(ctx context.Context) error { return s.srv.Shutdown(ctx) }

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(v)
}

func (s *Server) healthz(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, map[string]any{
		"ok":        true,
		"connected": s.consumer.IsConnected(),
		"timestamp": time.Now().UTC().Format(time.RFC3339),
	})
}

func (s *Server) metrics(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, s.consumer.Stats())
}

func (s *Server) state(w http.ResponseWriter, r *http.Request) {
	snap, err := store.SnapshotSince(r.Context(), s.online.Seconds())
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	rows, _ := store.Count(r.Context(), "events")
	raw, _ := store.Count(r.Context(), "raw_messages")
	quar, _ := store.Count(r.Context(), "quarantine")
	writeJSON(w, map[string]any{
		"devices":          snap,
		"events_total":     rows,
		"raw_messages":     raw,
		"quarantine_total": quar,
	})
}

func (s *Server) faultsHandler(w http.ResponseWriter, _ *http.Request) {
	fail, dropAck := s.faults.List()
	writeJSON(w, map[string]any{"commit_failures": fail, "drop_ack": dropAck})
}

func (s *Server) arm(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST required", http.StatusMethodNotAllowed)
		return
	}
	dev := r.URL.Query().Get("device")
	if dev == "" {
		http.Error(w, "device query param required", http.StatusBadRequest)
		return
	}
	s.faults.Arm(dev)
	fail, dropAck := s.faults.List()
	writeJSON(w, map[string]any{"commit_failures": fail, "drop_ack": dropAck})
}

func (s *Server) dropAck(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST required", http.StatusMethodNotAllowed)
		return
	}
	dev := r.URL.Query().Get("device")
	if dev == "" {
		http.Error(w, "device query param required", http.StatusBadRequest)
		return
	}
	s.faults.ArmDropAck(dev)
	fail, d := s.faults.List()
	writeJSON(w, map[string]any{"commit_failures": fail, "drop_ack": d})
}

func (s *Server) clear(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST required", http.StatusMethodNotAllowed)
		return
	}
	dev := r.URL.Query().Get("device")
	if dev == "" {
		dev = "*"
	}
	s.faults.Clear(dev)
	fail, dropAck := s.faults.List()
	writeJSON(w, map[string]any{"commit_failures": fail, "drop_ack": dropAck})
}
