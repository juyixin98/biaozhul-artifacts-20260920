// Package server exposes the clustering engine over a small, JSON-only HTTP
// API. No external dependencies; responses are deterministic (collections are
// slices in ID/sequence order, never Go maps).
package server

import (
	"encoding/json"
	"io"
	"log"
	"net/http"
	"strconv"
	"strings"

	"logcluster/internal/engine"
)

// Server wraps an engine with HTTP handlers.
type Server struct {
	eng    *engine.Engine
	logger *log.Logger
}

// New builds a server.
func New(eng *engine.Engine, logger *log.Logger) *Server {
	if logger == nil {
		logger = log.Default()
	}
	return &Server{eng: eng, logger: logger}
}

// Routes returns the wired mux.
func (s *Server) Routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", s.health)
	mux.HandleFunc("POST /v1/ingest", s.ingest)
	mux.HandleFunc("GET /v1/templates", s.templates)
	mux.HandleFunc("GET /v1/templates/{id}", s.templateByID)
	mux.HandleFunc("GET /v1/events", s.events)
	mux.HandleFunc("GET /v1/evicted", s.evicted)
	mux.HandleFunc("GET /v1/metrics", s.metrics)
	mux.HandleFunc("POST /v1/snapshot", s.snapshot)
	return mux
}

func (s *Server) health(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

type ingestRequest struct {
	Line  string   `json:"line"`
	Lines []string `json:"lines"`
}

type ingestResult struct {
	Seq       int64  `json:"seq"`
	Line      string `json:"line"`
	ClusterID int    `json:"cluster_id"`
	Template  string `json:"template"`
	Version   int    `json:"version"`
}

func (s *Server) ingest(w http.ResponseWriter, r *http.Request) {
	var lines []string

	ct := r.Header.Get("Content-Type")
	switch {
	case strings.Contains(ct, "application/json") || ct == "":
		body := http.MaxBytesReader(w, r.Body, 8<<20)
		defer body.Close()
		raw, err := io.ReadAll(body)
		if err != nil {
			writeError(w, http.StatusBadRequest, "read body: "+err.Error())
			return
		}
		var req ingestRequest
		if err := json.Unmarshal(raw, &req); err != nil {
			writeError(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
			return
		}
		if len(req.Lines) > 0 {
			lines = req.Lines
		} else if strings.TrimSpace(req.Line) != "" {
			lines = []string{req.Line}
		}
	case strings.Contains(ct, "text/plain"):
		body := http.MaxBytesReader(w, r.Body, 8<<20)
		defer body.Close()
		raw, err := io.ReadAll(body)
		if err != nil {
			writeError(w, http.StatusBadRequest, "read body: "+err.Error())
			return
		}
		for _, ln := range strings.Split(string(raw), "\n") {
			if strings.TrimSpace(ln) != "" {
				lines = append(lines, ln)
			}
		}
	default:
		writeError(w, http.StatusUnsupportedMediaType, "use application/json or text/plain")
		return
	}

	if len(lines) == 0 {
		writeError(w, http.StatusBadRequest, "no log lines provided")
		return
	}

	results := make([]ingestResult, 0, len(lines))
	var rejected int
	for _, ln := range lines {
		ev, err := s.eng.Ingest(ln)
		if err != nil {
			rejected++
			continue
		}
		results = append(results, ingestResult{
			Seq:       ev.Seq,
			Line:      ev.Line,
			ClusterID: ev.ClusterID,
			Template:  ev.Template,
			Version:   ev.Version,
		})
	}
	if len(results) == 0 {
		writeError(w, http.StatusBadRequest, "all lines rejected (empty?)")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ingested": len(results),
		"rejected": rejected,
		"results":  results,
	})
}

func (s *Server) templates(w http.ResponseWriter, r *http.Request) {
	tpls := s.eng.Templates()
	writeJSON(w, http.StatusOK, map[string]any{
		"count":     len(tpls),
		"templates": tpls,
	})
}

func (s *Server) templateByID(w http.ResponseWriter, r *http.Request) {
	id, ok := parseID(w, r.PathValue("id"))
	if !ok {
		return
	}
	t, found := s.eng.Cluster(id)
	if !found {
		writeError(w, http.StatusNotFound, "template not found")
		return
	}
	writeJSON(w, http.StatusOK, t)
}

func (s *Server) events(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	limit, _ := strconv.Atoi(q.Get("limit"))
	sub := q.Get("q")
	var cid int
	hasCluster := false
	if v := q.Get("cluster_id"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n < 1 {
			writeError(w, http.StatusBadRequest, "invalid cluster_id")
			return
		}
		cid, hasCluster = n, true
	}
	events := s.eng.Query(cid, hasCluster, sub, limit)
	writeJSON(w, http.StatusOK, map[string]any{
		"count":  len(events),
		"events": events,
	})
}

func (s *Server) evicted(w http.ResponseWriter, r *http.Request) {
	ev := s.eng.Evicted()
	writeJSON(w, http.StatusOK, map[string]any{
		"count":   len(ev),
		"evicted": ev,
	})
}

func (s *Server) metrics(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, s.eng.Stats())
}

type snapshotRequest struct {
	Path string `json:"path"`
}

func (s *Server) snapshot(w http.ResponseWriter, r *http.Request) {
	var req snapshotRequest
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20)).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
		return
	}
	if strings.TrimSpace(req.Path) == "" {
		writeError(w, http.StatusBadRequest, "path is required")
		return
	}
	if err := s.eng.Save(req.Path); err != nil {
		s.logger.Printf("snapshot save failed: %v", err)
		writeError(w, http.StatusInternalServerError, "snapshot failed: "+err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "saved", "path": req.Path})
}

// --- helpers ---

func parseID(w http.ResponseWriter, v string) (int, bool) {
	id, err := strconv.Atoi(v)
	if err != nil || id < 1 {
		writeError(w, http.StatusBadRequest, "invalid id")
		return 0, false
	}
	return id, true
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	if err := enc.Encode(v); err != nil {
		// Headers already sent; nothing else to do but log.
		log.Printf("write response: %v", err)
	}
}

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]any{"error": msg, "status": status})
}
