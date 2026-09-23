// Package api exposes the dependency resolver over a local HTTP JSON API.
// It never connects to a cloud platform or the network for package data;
// registries are supplied inline in each request.
package api

import (
	"bytes"
	"encoding/json"
	"net/http"

	"depresolve/internal/cache"
	"depresolve/internal/solver"
)

// SolveRequest is the POST /v1/solve request body.
type SolveRequest struct {
	Registry solver.Registry   `json:"registry"`
	Root     map[string]string `json:"root"`
	Options  SolveOptions      `json:"options,omitempty"`
}

// SolveOptions toggles solver/cache behaviour.
type SolveOptions struct {
	// DisableCache skips reading/writing the on-disk cache.
	DisableCache bool `json:"disable_cache,omitempty"`
}

// SolveResponse wraps solver.Result with request metadata.
type SolveResponse struct {
	solver.Result
	Cached bool `json:"cached,omitempty"`
}

// Server wires the HTTP routes.
type Server struct {
	cache *cache.Cache // may be nil
	mux   *http.ServeMux
}

// NewServer builds a server backed by c (may be nil to disable caching).
func NewServer(c *cache.Cache) *Server {
	s := &Server{cache: c, mux: http.NewServeMux()}
	s.mux.HandleFunc("/healthz", s.health)
	s.mux.HandleFunc("/v1/solve", s.solve)
	return s
}

// Handler exposes the router.
func (s *Server) Handler() http.Handler { return s.mux }

func (s *Server) health(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *Server) solve(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	var req SolveRequest
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid request body: "+err.Error())
		return
	}
	if len(req.Registry) == 0 {
		writeError(w, http.StatusBadRequest, "registry must be non-empty")
		return
	}
	if len(req.Root) == 0 {
		writeError(w, http.StatusBadRequest, "root must list at least one package")
		return
	}

	useCache := s.cache != nil && !req.Options.DisableCache
	var key string
	if useCache {
		k, err := cache.Key(req)
		if err != nil {
			writeError(w, http.StatusInternalServerError, "cache key error: "+err.Error())
			return
		}
		key = k
		if raw, ok := s.cache.Get(key); ok {
			var cached solver.Result
			if err := json.Unmarshal(raw, &cached); err == nil {
				writeJSON(w, http.StatusOK, SolveResponse{Result: cached, Cached: true})
				return
			}
		}
	}

	sol, err := solver.NewSolver(req.Registry, req.Root)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	result := sol.Solve()
	stored, err := MarshalJSON(result)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "marshal error: "+err.Error())
		return
	}
	if useCache {
		_ = s.cache.Put(key, stored)
	}
	writeJSON(w, http.StatusOK, SolveResponse{Result: result, Cached: false})
}

// MarshalJSON renders v as indented JSON without HTML escaping, so
// constraint strings like ">=1.0.0 <2.0.0" stay readable.
func MarshalJSON(v any) ([]byte, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	enc.SetIndent("", "  ")
	if err := enc.Encode(v); err != nil {
		return nil, err
	}
	return bytes.TrimRight(buf.Bytes(), "\n"), nil
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	out, err := MarshalJSON(v)
	if err != nil {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write([]byte(`{"error":"marshal error"}`))
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(out)
}

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}
