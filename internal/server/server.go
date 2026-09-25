// Package server exposes the build engine over a local HTTP JSON API.
// It binds to 127.0.0.1 by default, performs no network calls itself and
// only executes commands explicitly declared in registered projects.
package server

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"sort"
	"sync"
	"time"

	"cdag/internal/cache"
	"cdag/internal/engine"
	"cdag/internal/graph"
	"cdag/internal/spec"
)

// Server holds registered projects in memory and routes HTTP requests.
type Server struct {
	mu       sync.RWMutex
	projects map[string]*storedProject
	eng      *engine.Engine
}

type storedProject struct {
	p         *spec.Project
	createdAt time.Time
}

// New creates a server backed by the given cache.
func New(store *cache.Cache) *Server {
	return &Server{
		projects: map[string]*storedProject{},
		eng:      engine.New(store),
	}
}

// Routes returns the http.Handler with all v1 routes registered.
func (s *Server) Routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", s.handleHealth)
	mux.HandleFunc("GET /v1/projects", s.handleListProjects)
	mux.HandleFunc("POST /v1/projects", s.handleCreateProject)
	mux.HandleFunc("GET /v1/projects/{id}", s.handleGetProject)
	mux.HandleFunc("POST /v1/builds", s.handleBuild)
	mux.HandleFunc("POST /v1/validate", s.handleValidate)
	return mux
}

type errorBody struct {
	Error struct {
		Code    string   `json:"code"`
		Message string   `json:"message"`
		Cycle   []string `json:"cycle,omitempty"`
	} `json:"error"`
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

func writeError(w http.ResponseWriter, status int, code, message string, cycle []string) {
	var b errorBody
	b.Error.Code = code
	b.Error.Message = message
	b.Error.Cycle = cycle
	writeJSON(w, status, b)
}

// writeSpecError maps a spec/graph validation error onto a 400 response,
// preserving the cycle path when present.
func writeSpecError(w http.ResponseWriter, err error) {
	var ge *graph.Error
	if errors.As(err, &ge) {
		switch ge.Kind {
		case "cycle":
			writeError(w, http.StatusBadRequest, "cycle", ge.Error(), ge.Cycle)
			return
		case "unknown_dependency", "unknown_target":
			writeError(w, http.StatusBadRequest, "unknown_reference", ge.Error(), nil)
			return
		case "path_escape", "invalid_path":
			writeError(w, http.StatusBadRequest, "invalid_path", ge.Error(), nil)
			return
		default:
			writeError(w, http.StatusBadRequest, "invalid_project", ge.Error(), ge.Cycle)
			return
		}
	}
	writeError(w, http.StatusBadRequest, "invalid_project", err.Error(), nil)
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

type projectSummary struct {
	ID        string    `json:"id"`
	Workdir   string    `json:"workdir"`
	NodeCount int       `json:"node_count"`
	CreatedAt time.Time `json:"created_at"`
}

func (s *Server) handleListProjects(w http.ResponseWriter, r *http.Request) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make([]projectSummary, 0, len(s.projects))
	for id, sp := range s.projects {
		out = append(out, projectSummary{
			ID: id, Workdir: sp.p.Workdir, NodeCount: len(sp.p.Graph.Nodes), CreatedAt: sp.createdAt,
		})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ID < out[j].ID })
	writeJSON(w, http.StatusOK, map[string]any{"projects": out})
}

func (s *Server) handleCreateProject(w http.ResponseWriter, r *http.Request) {
	var p spec.Project
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20)).Decode(&p); err != nil {
		writeError(w, http.StatusBadRequest, "invalid_json", "request body is not valid JSON: "+err.Error(), nil)
		return
	}
	if err := p.Validate(); err != nil {
		writeSpecError(w, err)
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, exists := s.projects[p.ID]; exists {
		writeError(w, http.StatusConflict, "project_exists",
			fmt.Sprintf("project %q already registered", p.ID), nil)
		return
	}
	s.projects[p.ID] = &storedProject{p: &p, createdAt: time.Now().UTC()}
	writeJSON(w, http.StatusCreated, map[string]any{
		"id":         p.ID,
		"workdir":    p.Workdir,
		"node_count": len(p.Graph.Nodes),
		"registered": true,
	})
}

func (s *Server) handleGetProject(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	s.mu.RLock()
	sp, ok := s.projects[id]
	s.mu.RUnlock()
	if !ok {
		writeError(w, http.StatusNotFound, "not_found", "project not found: "+id, nil)
		return
	}
	writeJSON(w, http.StatusOK, sp.p)
}

type buildRequest struct {
	ProjectID string   `json:"project_id"`
	Targets   []string `json:"targets,omitempty"`
}

func (s *Server) handleBuild(w http.ResponseWriter, r *http.Request) {
	var req buildRequest
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20)).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid_json", "request body is not valid JSON: "+err.Error(), nil)
		return
	}
	if req.ProjectID == "" {
		writeError(w, http.StatusBadRequest, "missing_project", "project_id is required", nil)
		return
	}
	s.mu.RLock()
	sp, ok := s.projects[req.ProjectID]
	s.mu.RUnlock()
	if !ok {
		writeError(w, http.StatusNotFound, "not_found", "project not found: "+req.ProjectID, nil)
		return
	}
	report, err := s.eng.Build(sp.p, req.Targets)
	if err != nil {
		writeSpecError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, report)
}

func (s *Server) handleValidate(w http.ResponseWriter, r *http.Request) {
	var p spec.Project
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20)).Decode(&p); err != nil {
		writeError(w, http.StatusBadRequest, "invalid_json", "request body is not valid JSON: "+err.Error(), nil)
		return
	}
	if err := p.Validate(); err != nil {
		writeSpecError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"valid":      true,
		"id":         p.ID,
		"node_count": len(p.Graph.Nodes),
	})
}
