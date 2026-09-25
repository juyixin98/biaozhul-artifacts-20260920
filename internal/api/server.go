// Package api exposes the JSON HTTP interface. It serves only localhost
// style JSON; there is no frontend and no outbound network use.
package api

import (
	"context"
	"encoding/json"
	"net/http"
	"time"

	"licensejudge/internal/judge"
	"licensejudge/internal/policy"
	"licensejudge/internal/runner"
)

// Server bundles dependencies for the HTTP handlers.
type Server struct {
	Policy *policy.Policy
	Runner *runner.Runner
}

// NewMux builds the routing mux.
func (s *Server) NewMux() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", s.handleHealth)
	mux.HandleFunc("GET /v1/policy", s.handleGetPolicy)
	mux.HandleFunc("POST /v1/judge", s.handleJudge)
	mux.HandleFunc("GET /v1/fixtures", s.handleListFixtures)
	mux.HandleFunc("POST /v1/fixtures/run", s.handleRunFixture)
	return logging(mux)
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

func writeError(w http.ResponseWriter, status int, code, message string) {
	writeJSON(w, status, map[string]string{"error": code, "message": message})
}

type judgeRequest struct {
	Expression string `json:"expression"`
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *Server) handleGetPolicy(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, s.Policy)
}

func (s *Server) handleJudge(w http.ResponseWriter, r *http.Request) {
	var req judgeRequest
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<16)).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid-json", err.Error())
		return
	}
	if req.Expression == "" {
		writeError(w, http.StatusBadRequest, "missing-expression", "field \"expression\" is required")
		return
	}
	res, err := judge.Evaluate(req.Expression, s.Policy)
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid-expression", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, res)
}

func (s *Server) handleListFixtures(w http.ResponseWriter, r *http.Request) {
	type fixtureInfo struct {
		Name        string `json:"name"`
		Description string `json:"description"`
	}
	infos := make([]fixtureInfo, 0)
	for _, c := range s.Runner.Describe() {
		infos = append(infos, fixtureInfo{Name: c.Name, Description: c.Description})
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"work_dir":  s.Runner.WorkDir(),
		"cache_dir": s.Runner.CacheDir(),
		"fixtures":  infos,
	})
}

type runFixtureRequest struct {
	Name    string `json:"name"`
	NoCache bool   `json:"no_cache"`
}

func (s *Server) handleRunFixture(w http.ResponseWriter, r *http.Request) {
	var req runFixtureRequest
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<16)).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid-json", err.Error())
		return
	}
	if req.Name == "" {
		writeError(w, http.StatusBadRequest, "missing-name", "field \"name\" is required")
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 60*time.Second)
	defer cancel()
	res, err := s.Runner.Run(ctx, req.Name, !req.NoCache)
	if err != nil {
		writeError(w, http.StatusBadRequest, "fixture-not-allowed", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, res)
}
