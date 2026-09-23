// Package server exposes the multiline merger over HTTP and persists
// finalized entries through a Sink.
package server

import (
	"encoding/json"
	"log"
	"net/http"
	"regexp"
	"strconv"
	"time"

	"logpipe/internal/merger"
)

// Sink is what finalized entries are handed to (typically *store.FileStore).
type Sink interface {
	Append(e merger.Entry) (merger.Entry, error)
	Query(source string, limit int) []merger.Entry
}

// Config configures a Server.
type Config struct {
	StartRule     *regexp.Regexp
	Timeout       time.Duration
	MaxBytes      int
	SweepInterval time.Duration
}

// Server holds the HTTP handlers and the merger lifecycle.
type Server struct {
	mux    *http.ServeMux
	mg     *merger.Merger
	sink   Sink
	logger *log.Logger
}

// New wires a merger to the sink and registers routes.
func New(cfg Config, sink Sink, logger *log.Logger) *Server {
	s := &Server{sink: sink, logger: logger}
	s.mg = merger.New(merger.Config{
		StartRule:     cfg.StartRule,
		Timeout:       cfg.Timeout,
		MaxBytes:      cfg.MaxBytes,
		SweepInterval: cfg.SweepInterval,
		OnFlush: func(e merger.Entry) {
			if _, err := sink.Append(e); err != nil {
				logger.Printf("persist entry from source %q: %v", e.Source, err)
			}
		},
	})
	s.routes()
	return s
}

func (s *Server) routes() {
	s.mux = http.NewServeMux()
	s.mux.HandleFunc("POST /ingest", s.handleIngest)
	s.mux.HandleFunc("GET /entries", s.handleQuery)
	s.mux.HandleFunc("POST /admin/force-flush", s.handleForceFlush)
	s.mux.HandleFunc("GET /healthz", s.handleHealth)
}

// Handler returns the HTTP handler.
func (s *Server) Handler() http.Handler { return s.mux }

// Close shuts down the merger, flushing pending entries to the sink.
func (s *Server) Close() { s.mg.Close() }

// lineDTO is one line on the ingest API. Ts is optional (RFC3339).
type lineDTO struct {
	Source string `json:"source"`
	PID    *int   `json:"pid,omitempty"`
	Text   string `json:"text"`
	Ts     string `json:"ts,omitempty"`
}

// ingestRequest accepts either a single line object or an array of them.
type ingestRequest struct {
	lines []lineDTO
}

func (r *ingestRequest) UnmarshalJSON(data []byte) error {
	if len(data) > 0 && data[0] == '[' {
		return json.Unmarshal(data, &r.lines)
	}
	var one lineDTO
	if err := json.Unmarshal(data, &one); err != nil {
		return err
	}
	r.lines = []lineDTO{one}
	return nil
}

type ingestResponse struct {
	Accepted int `json:"accepted"`
}

func (s *Server) handleIngest(w http.ResponseWriter, r *http.Request) {
	r.Body = http.MaxBytesReader(w, r.Body, 4<<20)
	var req ingestRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
		return
	}
	if len(req.lines) == 0 {
		writeError(w, http.StatusBadRequest, "no log lines in body")
		return
	}

	now := time.Now()
	for i, dto := range req.lines {
		line := merger.Line{Source: dto.Source, PID: dto.PID, Text: dto.Text}
		if dto.Ts != "" {
			ts, err := time.Parse(time.RFC3339, dto.Ts)
			if err != nil {
				writeError(w, http.StatusBadRequest,
					"line "+strconv.Itoa(i)+": ts must be RFC3339: "+err.Error())
				return
			}
			line.Ts = ts
		} else {
			line.Ts = now
		}
		if err := s.mg.Ingest(line); err != nil {
			writeError(w, http.StatusBadRequest,
				"line "+strconv.Itoa(i)+": "+err.Error())
			return
		}
	}
	writeJSON(w, http.StatusAccepted, ingestResponse{Accepted: len(req.lines)})
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *Server) handleForceFlush(w http.ResponseWriter, r *http.Request) {
	n := s.mg.ForceFlush()
	writeJSON(w, http.StatusOK, map[string]int{"flushed": n})
}

func (s *Server) handleQuery(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	source := q.Get("source")
	limit := 0
	if v := q.Get("limit"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n < 0 {
			writeError(w, http.StatusBadRequest, "limit must be a non-negative integer")
			return
		}
		limit = n
	}
	entries := s.sink.Query(source, limit)
	writeJSON(w, http.StatusOK, map[string]any{"entries": entries, "count": len(entries)})
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, code int, msg string) {
	writeJSON(w, code, map[string]string{"error": msg})
}
