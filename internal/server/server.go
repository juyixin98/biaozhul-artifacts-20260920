// Package server exposes ring management, key routing and migration-plan
// generation over a JSON HTTP API. State is in-memory.
package server

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"sort"
	"sync"

	"consistenthash/internal/plan"
	"consistenthash/internal/ring"
)

// Server holds an in-memory set of named rings.
type Server struct {
	mu    sync.RWMutex
	rings map[string]*ring.Ring

	mux *http.ServeMux
}

// New constructs a Server with its routes registered.
func New() *Server {
	s := &Server{rings: make(map[string]*ring.Ring)}
	mux := http.NewServeMux()

	mux.HandleFunc("GET /health", s.handleHealth)
	mux.HandleFunc("GET /api/rings", s.handleListRings)
	mux.HandleFunc("POST /api/rings", s.handleCreateRing)
	mux.HandleFunc("GET /api/rings/{name}", s.handleGetRing)
	mux.HandleFunc("GET /api/rings/{name}/route", s.handleRouteGet)
	mux.HandleFunc("POST /api/rings/{name}/route", s.handleRouteBatch)
	mux.HandleFunc("POST /api/plan", s.handlePlan)

	s.mux = mux
	return s
}

// Handler returns the root http.Handler.
func (s *Server) Handler() http.Handler { return s.mux }

type ringRequest struct {
	Name                string      `json:"name"`
	Nodes               []ring.Node `json:"nodes"`
	VnodesPerWeightUnit int         `json:"vnodes_per_weight_unit"`
}

type errorResponse struct {
	Error string `json:"error"`
}

func writeError(w http.ResponseWriter, code int, format string, args ...any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(errorResponse{Error: fmt.Sprintf(format, args...)})
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *Server) handleListRings(w http.ResponseWriter, r *http.Request) {
	s.mu.RLock()
	names := make([]string, 0, len(s.rings))
	for name := range s.rings {
		names = append(names, name)
	}
	s.mu.RUnlock()
	sort.Strings(names)
	writeJSON(w, http.StatusOK, map[string]any{"rings": names})
}

func (s *Server) handleCreateRing(w http.ResponseWriter, r *http.Request) {
	var req ringRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body: %v", err)
		return
	}
	if req.Name == "" {
		writeError(w, http.StatusBadRequest, "name is required")
		return
	}

	built, err := ring.New(req.Name, req.Nodes, req.VnodesPerWeightUnit)
	if err != nil {
		writeError(w, http.StatusBadRequest, "%v", err)
		return
	}

	s.mu.Lock()
	if _, exists := s.rings[req.Name]; exists {
		s.mu.Unlock()
		writeError(w, http.StatusConflict, "ring %q already exists", req.Name)
		return
	}
	s.rings[req.Name] = built
	s.mu.Unlock()

	writeJSON(w, http.StatusCreated, map[string]any{"ring": built.Summary()})
}

func (s *Server) getRing(name string) (*ring.Ring, bool) {
	s.mu.RLock()
	rg, ok := s.rings[name]
	s.mu.RUnlock()
	return rg, ok
}

func (s *Server) handleGetRing(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("name")
	rg, ok := s.getRing(name)
	if !ok {
		writeError(w, http.StatusNotFound, "ring %q not found", name)
		return
	}
	resp := map[string]any{"ring": rg.Summary()}
	if r.URL.Query().Get("vnodes") == "1" || r.URL.Query().Get("vnodes") == "true" {
		resp["vnodes"] = rg.VNodes()
	}
	writeJSON(w, http.StatusOK, resp)
}

type routeResult struct {
	Key         string `json:"key"`
	Position    string `json:"position"`
	PositionHex string `json:"position_hex"`
	Owner       string `json:"owner"`
}

func routeOne(rg *ring.Ring, key string) routeResult {
	h := ring.Hash([]byte(key))
	return routeResult{
		Key:         key,
		Position:    fmt.Sprintf("%d", h),
		PositionHex: "0x" + fmt.Sprintf("%016x", h),
		Owner:       rg.Owner(key),
	}
}

func (s *Server) handleRouteGet(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("name")
	rg, ok := s.getRing(name)
	if !ok {
		writeError(w, http.StatusNotFound, "ring %q not found", name)
		return
	}
	keys := r.URL.Query()["key"]
	if len(keys) == 0 {
		writeError(w, http.StatusBadRequest, "at least one key query parameter is required")
		return
	}
	results := make([]routeResult, 0, len(keys))
	for _, k := range keys {
		results = append(results, routeOne(rg, k))
	}
	writeJSON(w, http.StatusOK, map[string]any{"results": results})
}

type routeBatchRequest struct {
	Keys []string `json:"keys"`
}

func (s *Server) handleRouteBatch(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("name")
	rg, ok := s.getRing(name)
	if !ok {
		writeError(w, http.StatusNotFound, "ring %q not found", name)
		return
	}
	var req routeBatchRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body: %v", err)
		return
	}
	if len(req.Keys) == 0 {
		writeError(w, http.StatusBadRequest, "keys must not be empty")
		return
	}
	results := make([]routeResult, 0, len(req.Keys))
	for _, k := range req.Keys {
		results = append(results, routeOne(rg, k))
	}
	writeJSON(w, http.StatusOK, map[string]any{"results": results})
}

type planRequest struct {
	Old       json.RawMessage `json:"old"`
	New       json.RawMessage `json:"new"`
	ProbeKeys []string        `json:"probe_keys"`
}

type planProbeResult struct {
	Key         string `json:"key"`
	Position    string `json:"position"`
	PositionHex string `json:"position_hex"`
	OldOwner    string `json:"old_owner"`
	NewOwner    string `json:"new_owner"`
	Migrates    bool   `json:"migrates"`
}

// resolveSide accepts either a quoted stored ring name ("cluster-a") or an
// inline ring definition object.
func (s *Server) resolveSide(raw json.RawMessage) (*ring.Ring, error) {
	if len(raw) == 0 {
		return nil, errors.New("ring side is missing")
	}
	var name string
	if err := json.Unmarshal(raw, &name); err == nil {
		rg, ok := s.getRing(name)
		if !ok {
			return nil, fmt.Errorf("stored ring %q not found", name)
		}
		return rg, nil
	}
	var req ringRequest
	if err := json.Unmarshal(raw, &req); err != nil {
		return nil, fmt.Errorf("ring side must be a name string or a ring object: %v", err)
	}
	if req.Name == "" {
		req.Name = "inline"
	}
	rg, err := ring.New(req.Name, req.Nodes, req.VnodesPerWeightUnit)
	if err != nil {
		return nil, err
	}
	return rg, nil
}

func (s *Server) handlePlan(w http.ResponseWriter, r *http.Request) {
	var req planRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body: %v", err)
		return
	}

	s.mu.RLock()
	defer s.mu.RUnlock()

	oldRing, err := s.resolveSide(req.Old)
	if err != nil {
		writeError(w, http.StatusBadRequest, "%v", err)
		return
	}
	newRing, err := s.resolveSide(req.New)
	if err != nil {
		writeError(w, http.StatusBadRequest, "%v", err)
		return
	}

	p, err := plan.Build(oldRing, newRing)
	if err != nil {
		writeError(w, http.StatusBadRequest, "%v", err)
		return
	}

	probes := make([]planProbeResult, 0, len(req.ProbeKeys))
	for _, k := range req.ProbeKeys {
		h := ring.Hash([]byte(k))
		oldOwner := oldRing.Owner(k)
		newOwner := newRing.Owner(k)
		probes = append(probes, planProbeResult{
			Key:         k,
			Position:    fmt.Sprintf("%d", h),
			PositionHex: "0x" + fmt.Sprintf("%016x", h),
			OldOwner:    oldOwner,
			NewOwner:    newOwner,
			Migrates:    oldOwner != newOwner,
		})
	}

	writeJSON(w, http.StatusOK, map[string]any{"plan": p, "probes": probes})
}
