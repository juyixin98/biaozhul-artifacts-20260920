// Package api exposes the scaler over net/http.
package api

import (
	"encoding/json"
	"errors"
	"net/http"
	"time"

	"autoscaler/internal/scaler"
)

// Server wraps a Scaler with HTTP handlers.
type Server struct {
	s *scaler.Scaler
}

// NewServer creates a Server.
func NewServer(s *scaler.Scaler) *Server { return &Server{s: s} }

// Handler returns the root handler with all routes registered.
func (srv *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", srv.healthz)
	mux.HandleFunc("GET /v1/config", srv.getConfig)
	mux.HandleFunc("PUT /v1/config", srv.putConfig)
	mux.HandleFunc("POST /v1/metrics", srv.postMetrics)
	mux.HandleFunc("POST /v1/evaluate", srv.evaluate)
	mux.HandleFunc("GET /v1/state", srv.state)
	mux.HandleFunc("GET /v1/decisions", srv.decisions)
	mux.HandleFunc("POST /v1/reset", srv.reset)
	return mux
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(code)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

func writeErr(w http.ResponseWriter, code int, msg string) {
	writeJSON(w, code, map[string]string{"error": msg})
}

func decode(w http.ResponseWriter, r *http.Request, v any) error {
	if r.Body == nil {
		return errors.New("empty body")
	}
	defer r.Body.Close()
	dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20))
	if err := dec.Decode(v); err != nil {
		return err
	}
	return nil
}

func (srv *Server) healthz(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (srv *Server) getConfig(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, srv.s.Config())
}

func (srv *Server) putConfig(w http.ResponseWriter, r *http.Request) {
	var cfg scaler.Config
	if err := decode(w, r, &cfg); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
		return
	}
	if err := srv.s.UpdateConfig(cfg); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid config: "+err.Error())
		return
	}
	writeJSON(w, http.StatusOK, srv.s.Config())
}

type metricsRequest struct {
	Samples []scaler.Sample `json:"samples"`
}

func (srv *Server) postMetrics(w http.ResponseWriter, r *http.Request) {
	var req metricsRequest
	if err := decode(w, r, &req); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
		return
	}
	if len(req.Samples) == 0 {
		writeErr(w, http.StatusBadRequest, "no samples provided")
		return
	}
	n := srv.s.Ingest(req.Samples)
	writeJSON(w, http.StatusOK, map[string]int{"ingested": n})
}

type evaluateRequest struct {
	Time string `json:"time"` // RFC3339; empty = server clock
}

func (srv *Server) evaluate(w http.ResponseWriter, r *http.Request) {
	now := time.Now()
	var req evaluateRequest
	if r.Body != nil && r.ContentLength != 0 {
		if err := decode(w, r, &req); err != nil {
			writeErr(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
			return
		}
		if req.Time != "" {
			t, err := time.Parse(time.RFC3339, req.Time)
			if err != nil {
				writeErr(w, http.StatusBadRequest, "invalid time (want RFC3339): "+err.Error())
				return
			}
			now = t
		}
	}
	writeJSON(w, http.StatusOK, srv.s.Evaluate(now))
}

func (srv *Server) state(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, srv.s.State())
}

func (srv *Server) decisions(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"decisions": srv.s.Decisions()})
}

type resetRequest struct {
	Replicas int `json:"replicas"`
}

func (srv *Server) reset(w http.ResponseWriter, r *http.Request) {
	var req resetRequest
	if err := decode(w, r, &req); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
		return
	}
	srv.s.Reset(req.Replicas)
	writeJSON(w, http.StatusOK, srv.s.State())
}
