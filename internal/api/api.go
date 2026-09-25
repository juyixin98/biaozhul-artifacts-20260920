// Package api exposes the cardinality-budgeted store over HTTP.
package api

import (
	"encoding/json"
	"errors"
	"io"
	"log"
	"net/http"

	"cardinalitybudget/internal/store"
)

// Flusher is implemented by a persistence-backed server so /debug/flush can
// force a snapshot write.
type Flusher interface {
	Flush() error
}

// Server holds HTTP dependencies.
type Server struct {
	Store   *store.Store
	Logger  *log.Logger
	Flusher Flusher // may be nil
	mux     *http.ServeMux
}

// NewServer wires the routes.
func NewServer(st *store.Store, logger *log.Logger, flusher Flusher) *Server {
	if logger == nil {
		logger = log.New(io.Discard, "", 0)
	}
	srv := &Server{Store: st, Logger: logger, Flusher: flusher}

	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", srv.handleHealth)
	mux.HandleFunc("/api/v1/ingest", srv.handleIngest)
	mux.HandleFunc("/api/v1/metrics", srv.handleMetrics)
	mux.HandleFunc("/api/v1/metrics/", srv.handleMetric)
	mux.HandleFunc("/api/v1/stats", srv.handleStats)
	mux.HandleFunc("/debug/flush", srv.handleFlush)
	srv.mux = mux
	return srv
}

// Handler returns the root http.Handler.
func (s *Server) Handler() http.Handler { return s.mux }

const (
	// MaxSamplesPerRequest bounds a single ingest payload.
	MaxSamplesPerRequest = 400
	// MaxBodyBytes bounds raw request size: labels + envelope for 400 samples.
	MaxBodyBytes = 8 << 20
)

type ingestRequest struct {
	Samples []store.Sample `json:"samples"`
}

type ingestResponse struct {
	Received int             `json:"received"`
	Accepted int             `json:"accepted"`
	Rejected int             `json:"rejected"`
	Overflow int             `json:"overflow"`
	Outcomes []store.Outcome `json:"outcomes"`
}

type errorResponse struct {
	Error string `json:"error"`
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, errorResponse{Error: msg})
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "GET only")
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *Server) handleIngest(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "POST only")
		return
	}

	r.Body = http.MaxBytesReader(w, r.Body, MaxBodyBytes)
	var req ingestRequest
	dec := json.NewDecoder(r.Body)
	if err := dec.Decode(&req); err != nil {
		var maxErr *http.MaxBytesError
		if errors.As(err, &maxErr) {
			writeError(w, http.StatusRequestEntityTooLarge, "request body too large")
			return
		}
		writeError(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
		return
	}
	if len(req.Samples) == 0 {
		writeError(w, http.StatusBadRequest, "samples must not be empty")
		return
	}
	if len(req.Samples) > MaxSamplesPerRequest {
		writeError(w, http.StatusBadRequest, "too many samples in one request (limit 400)")
		return
	}

	outcomes := s.Store.IngestBatch(req.Samples)
	resp := ingestResponse{Received: len(outcomes), Outcomes: outcomes}
	for _, o := range outcomes {
		if o.Accepted {
			resp.Accepted++
			if o.Overflow {
				resp.Overflow++
			}
		} else {
			resp.Rejected++
		}
	}
	writeJSON(w, http.StatusOK, resp)
}

func (s *Server) handleMetrics(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "GET only")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"metrics": s.Store.Metrics()})
}

func (s *Server) handleMetric(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "GET only")
		return
	}
	name := r.URL.Path[len("/api/v1/metrics/"):]
	if name == "" {
		writeError(w, http.StatusNotFound, "metric name required")
		return
	}
	includeSeries := r.URL.Query().Get("series") == "1" || r.URL.Query().Get("series") == "true"
	v, ok := s.Store.Metric(name, includeSeries)
	if !ok {
		writeError(w, http.StatusNotFound, "unknown metric: "+name)
		return
	}
	writeJSON(w, http.StatusOK, v)
}

func (s *Server) handleStats(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "GET only")
		return
	}
	writeJSON(w, http.StatusOK, s.Store.Stats())
}

func (s *Server) handleFlush(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost && r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "POST or GET only")
		return
	}
	if s.Flusher == nil {
		writeError(w, http.StatusNotImplemented, "persistence disabled")
		return
	}
	if err := s.Flusher.Flush(); err != nil {
		s.Logger.Printf("flush failed: %v", err)
		writeError(w, http.StatusInternalServerError, "flush failed: "+err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "flushed"})
}
