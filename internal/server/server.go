// Package server exposes the log clusterer over HTTP.
package server

import (
	"encoding/json"
	"errors"
	"log"
	"net/http"
	"strconv"
	"strings"
	"sync"

	"logcluster/internal/cluster"
)

// Server wraps a Clusterer with HTTP handlers. All clusterer access is
// serialized with a mutex.
type Server struct {
	mu       sync.Mutex
	cl       *cluster.Clusterer
	snapPath string // optional snapshot file; empty disables persistence
	mux      *http.ServeMux
}

func New(cl *cluster.Clusterer, snapshotPath string) *Server {
	s := &Server{cl: cl, snapPath: snapshotPath, mux: http.NewServeMux()}
	s.mux.HandleFunc("/healthz", s.handleHealth)
	s.mux.HandleFunc("POST /v1/logs", s.handleIngest)
	s.mux.HandleFunc("GET /v1/templates", s.handleListTemplates)
	s.mux.HandleFunc("GET /v1/templates/{id}", s.handleGetTemplate)
	s.mux.HandleFunc("GET /v1/stats", s.handleStats)
	s.mux.HandleFunc("POST /v1/snapshot", s.handleSnapshot)
	return s
}

func (s *Server) Handler() http.Handler { return s.mux }

// Clusterer returns the underlying clusterer (for tests and shutdown hooks).
func (s *Server) Clusterer() *cluster.Clusterer { return s.cl }

// SaveSnapshot persists the current state if a snapshot path is configured.
func (s *Server) SaveSnapshot() error {
	if s.snapPath == "" {
		return nil
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	return cluster.Save(s.cl, s.snapPath)
}

type ingestRequest struct {
	Line  string   `json:"line"`
	Lines []string `json:"lines"`
}

type ingestResult struct {
	Line       string `json:"line"`
	TemplateID int64  `json:"template_id"`
	Pattern    string `json:"pattern"`
	Version    int    `json:"version"`
	Created    bool   `json:"created"`
	Error      string `json:"error,omitempty"`
}

type ingestResponse struct {
	Ingested int            `json:"ingested"`
	Results  []ingestResult `json:"results"`
}

func (s *Server) handleIngest(w http.ResponseWriter, r *http.Request) {
	var req ingestRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
		return
	}
	lines := req.Lines
	if req.Line != "" {
		lines = append(lines, req.Line)
	}
	if len(lines) == 0 {
		writeError(w, http.StatusBadRequest, "no log lines provided")
		return
	}

	s.mu.Lock()
	defer s.mu.Unlock()
	resp := ingestResponse{Results: make([]ingestResult, 0, len(lines))}
	for _, line := range lines {
		a, err := s.cl.Ingest(line)
		res := ingestResult{Line: line}
		if err != nil {
			res.Error = err.Error()
		} else {
			res.TemplateID = a.TemplateID
			res.Pattern = a.Pattern
			res.Version = a.Version
			res.Created = a.Created
			resp.Ingested++
		}
		resp.Results = append(resp.Results, res)
	}
	writeJSON(w, http.StatusOK, resp)
}

func (s *Server) handleListTemplates(w http.ResponseWriter, r *http.Request) {
	s.mu.Lock()
	defer s.mu.Unlock()
	writeJSON(w, http.StatusOK, map[string]any{"templates": s.cl.Templates()})
}

func (s *Server) handleGetTemplate(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.ParseInt(r.PathValue("id"), 10, 64)
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid template id")
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	t, ok := s.cl.Get(id)
	if !ok {
		writeError(w, http.StatusNotFound, "template not found")
		return
	}
	writeJSON(w, http.StatusOK, t)
}

func (s *Server) handleStats(w http.ResponseWriter, r *http.Request) {
	s.mu.Lock()
	defer s.mu.Unlock()
	writeJSON(w, http.StatusOK, map[string]any{
		"stats":     s.cl.Stats(),
		"evictions": s.cl.Evictions(),
	})
}

func (s *Server) handleSnapshot(w http.ResponseWriter, r *http.Request) {
	if s.snapPath == "" {
		writeError(w, http.StatusConflict, "persistence not configured (start with -data)")
		return
	}
	if err := s.SaveSnapshot(); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"snapshot": s.snapPath})
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	enc.SetEscapeHTML(false) // keep <NUM> etc. readable in responses
	if err := enc.Encode(v); err != nil {
		log.Printf("encode response: %v", err)
	}
}

func writeError(w http.ResponseWriter, code int, msg string) {
	if msg == "" {
		msg = http.StatusText(code)
	}
	writeJSON(w, code, map[string]string{"error": strings.TrimSpace(msg)})
}

// LoadOrNew restores a clusterer from the snapshot file if it exists,
// otherwise returns a fresh one.
func LoadOrNew(cfg cluster.Config, snapshotPath string) (*cluster.Clusterer, error) {
	if snapshotPath == "" {
		return cluster.New(cfg), nil
	}
	snap, err := cluster.Load(snapshotPath)
	if err != nil {
		return nil, err
	}
	if snap == nil {
		return cluster.New(cfg), nil
	}
	cl := cluster.Restore(cfg, snap)
	if cl == nil {
		return nil, errors.New("restore returned nil clusterer")
	}
	return cl, nil
}
