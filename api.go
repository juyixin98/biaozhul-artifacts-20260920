package tailsampling

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
)

// Server wires HTTP endpoints to the aggregator.
type Server struct {
	agg *Aggregator
	mux *http.ServeMux
}

func NewServer(agg *Aggregator) *Server {
	s := &Server{agg: agg, mux: http.NewServeMux()}
	s.routes()
	return s
}

func (s *Server) Handler() http.Handler { return s.mux }

func (s *Server) routes() {
	s.mux.HandleFunc("POST /v1/spans", s.handleIngest)
	s.mux.HandleFunc("GET /v1/decisions/{traceID}", s.handleGetDecision)
	s.mux.HandleFunc("GET /v1/decisions", s.handleListDecisions)
	s.mux.HandleFunc("GET /v1/stats", s.handleStats)
	s.mux.HandleFunc("POST /admin/flush", s.handleFlush)
	s.mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
}

type ingestPayload struct {
	Spans []Span `json:"spans"`
}

func (s *Server) handleIngest(w http.ResponseWriter, r *http.Request) {
	body := http.MaxBytesReader(w, r.Body, 16<<20)
	raw, err := io.ReadAll(body)
	if err != nil {
		writeError(w, http.StatusBadRequest, "read body: "+err.Error())
		return
	}
	var p ingestPayload
	if err := json.Unmarshal(raw, &p); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
		return
	}
	if len(p.Spans) == 0 {
		writeError(w, http.StatusBadRequest, "spans must not be empty")
		return
	}
	for i, sp := range p.Spans {
		if err := validateSpan(sp); err != nil {
			writeError(w, http.StatusBadRequest, fmt.Sprintf("spans[%d]: %v", i, err))
			return
		}
	}
	report := s.agg.Ingest(p.Spans)
	status := http.StatusAccepted
	if report.Late > 0 {
		status = http.StatusOK // accepted, but note late arrivals
	}
	writeJSON(w, status, report)
}

func validateSpan(sp Span) error {
	if strings.TrimSpace(sp.TraceID) == "" {
		return errors.New("trace_id is required")
	}
	if strings.TrimSpace(sp.SpanID) == "" {
		return errors.New("span_id is required")
	}
	if sp.StartTimeMs <= 0 {
		return errors.New("start_time_ms must be > 0")
	}
	if sp.DurationMs < 0 {
		return errors.New("duration_ms must be >= 0")
	}
	switch sp.Status {
	case "", StatusOK, StatusError:
	default:
		return fmt.Errorf("status must be %q or %q, got %q", StatusOK, StatusError, sp.Status)
	}
	return nil
}

func (s *Server) handleGetDecision(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("traceID")
	d := s.agg.Decision(id)
	if d == nil {
		writeError(w, http.StatusNotFound, "no decision for trace "+id+" (not finalized yet or unknown)")
		return
	}
	writeJSON(w, http.StatusOK, d)
}

func (s *Server) handleListDecisions(w http.ResponseWriter, r *http.Request) {
	limit := 0
	if v := r.URL.Query().Get("limit"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n < 0 {
			writeError(w, http.StatusBadRequest, "limit must be a non-negative integer")
			return
		}
		limit = n
	}
	keptFilter := r.URL.Query().Get("kept")
	list := s.agg.ListDecisions(limit)
	if keptFilter != "" {
		want, err := strconv.ParseBool(keptFilter)
		if err != nil {
			writeError(w, http.StatusBadRequest, "kept must be a boolean")
			return
		}
		filtered := list[:0]
		for _, d := range list {
			if d.Kept == want {
				filtered = append(filtered, d)
			}
		}
		list = filtered
	}
	writeJSON(w, http.StatusOK, map[string]any{"count": len(list), "decisions": list})
}

func (s *Server) handleStats(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, s.agg.Stats())
}

func (s *Server) handleFlush(w http.ResponseWriter, r *http.Request) {
	decs := s.agg.Flush()
	writeJSON(w, http.StatusOK, map[string]any{
		"finalized": len(decs),
		"decisions": decs,
	})
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}
