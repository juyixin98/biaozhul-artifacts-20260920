// Package server exposes the log merger over HTTP.
package server

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"strconv"
	"time"

	"logmerge/internal/merger"
	"logmerge/internal/store"
)

// Server wires the HTTP API to a Merger and Store.
type Server struct {
	merger *merger.Merger
	store  *store.Store
	mux    *http.ServeMux
}

// New builds a Server. Flushed entries are appended to st.
func New(m *merger.Merger, st *store.Store) *Server {
	s := &Server{merger: m, store: st, mux: http.NewServeMux()}
	s.mux.HandleFunc("POST /ingest", s.handleIngest)
	s.mux.HandleFunc("GET /logs", s.handleLogs)
	s.mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		w.Write([]byte("ok"))
	})
	return s
}

// Handler returns the root HTTP handler.
func (s *Server) Handler() http.Handler { return s.mux }

type ingestLine struct {
	Source string `json:"source"`
	Line   string `json:"line"`
	// Time is optional (RFC3339); defaults to server receive time.
	Time time.Time `json:"time,omitempty"`
}

type ingestResponse struct {
	Accepted int `json:"accepted"`
}

func (s *Server) handleIngest(w http.ResponseWriter, r *http.Request) {
	body, err := io.ReadAll(io.LimitReader(r.Body, 16<<20))
	if err != nil {
		writeError(w, http.StatusBadRequest, "read body: "+err.Error())
		return
	}
	// Accept either a single object or an array.
	var lines []ingestLine
	if bytes.HasPrefix(bytes.TrimSpace(body), []byte("[")) {
		if err := json.Unmarshal(body, &lines); err != nil {
			writeError(w, http.StatusBadRequest, "invalid JSON array: "+err.Error())
			return
		}
	} else {
		var single ingestLine
		if err := json.Unmarshal(body, &single); err != nil {
			writeError(w, http.StatusBadRequest, "invalid JSON object: "+err.Error())
			return
		}
		lines = []ingestLine{single}
	}
	now := time.Now()
	for _, l := range lines {
		if l.Source == "" {
			writeError(w, http.StatusBadRequest, "every line needs a source")
			return
		}
		ts := l.Time
		if ts.IsZero() {
			ts = now
		}
		s.merger.Add(l.Source, l.Line, ts)
	}
	writeJSON(w, http.StatusAccepted, ingestResponse{Accepted: len(lines)})
}

type logsResponse struct {
	Entries []merger.Entry `json:"entries"`
	Count   int            `json:"count"`
}

func (s *Server) handleLogs(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	limit := 0
	if v := q.Get("limit"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n < 0 {
			writeError(w, http.StatusBadRequest, "limit must be a non-negative integer")
			return
		}
		limit = n
	}
	entries := s.store.List(q.Get("source"), limit)
	if entries == nil {
		entries = []merger.Entry{}
	}
	writeJSON(w, http.StatusOK, logsResponse{Entries: entries, Count: len(entries)})
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, code int, msg string) {
	writeJSON(w, code, map[string]string{"error": msg})
}
