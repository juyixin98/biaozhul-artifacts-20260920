// Package httpapi exposes the simulator over a small local HTTP API.
//
// Endpoints:
//
//	GET  /healthz
//	GET  /api/scenarios                 list built-in scenario names
//	POST /api/simulate                  run a spec supplied as JSON
//	GET  /api/scenarios/{name}          run a built-in scenario
//
// Every simulation response is a scheduler.Result; its "events" field is the
// structured scheduling timeline.
package httpapi

import (
	"encoding/json"
	"net/http"
	"sort"

	"pim/internal/scenario"
	"pim/internal/scheduler"
)

// Server bundles dependencies. The clock is injected per-run as a fresh
// virtual clock; callers wanting a custom clock should use the scheduler
// package directly (the library is the primary surface).
type Server struct {
	mux       *http.ServeMux
	scenarios map[string]func() scheduler.Spec
}

// NewServer builds the API handler with the built-in scenario catalogue.
func NewServer() *Server {
	s := &Server{scenarios: scenario.All()}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", s.health)
	mux.HandleFunc("GET /api/scenarios", s.listScenarios)
	mux.HandleFunc("POST /api/simulate", s.simulate)
	mux.HandleFunc("GET /api/scenarios/{name}", s.runScenario)
	s.mux = mux
	return s
}

// Handler exposes the configured routes.
func (s *Server) Handler() http.Handler { return s.mux }

func (s *Server) health(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *Server) listScenarios(w http.ResponseWriter, _ *http.Request) {
	names := make([]string, 0, len(s.scenarios))
	for n := range s.scenarios {
		names = append(names, n)
	}
	sort.Strings(names)
	specs := make([]map[string]any, 0, len(names))
	for _, n := range names {
		sp := s.scenarios[n]()
		specs = append(specs, map[string]any{
			"name":      n,
			"label":     sp.Name,
			"resources": sp.Resources,
			"options":   sp.Options,
			"tasks":     taskSummaries(sp),
		})
	}
	writeJSON(w, http.StatusOK, map[string]any{"scenarios": specs})
}

func taskSummaries(sp scheduler.Spec) []map[string]any {
	out := make([]map[string]any, 0, len(sp.Tasks))
	for _, t := range sp.Tasks {
		out = append(out, map[string]any{
			"id":          t.ID,
			"arrival":     t.Arrival,
			"priority":    t.Priority,
			"actionCount": len(t.Program.Actions),
		})
	}
	return out
}

func (s *Server) simulate(w http.ResponseWriter, r *http.Request) {
	var spec scheduler.Spec
	dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&spec); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON spec: "+err.Error())
		return
	}
	res, err := scheduler.Run(spec, nil, nil)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, res)
}

func (s *Server) runScenario(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("name")
	build, ok := s.scenarios[name]
	if !ok {
		writeError(w, http.StatusNotFound, "unknown scenario: "+name)
		return
	}
	res, err := scheduler.Run(build(), nil, nil)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, res)
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(code)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

func writeError(w http.ResponseWriter, code int, msg string) {
	writeJSON(w, code, map[string]any{"error": msg, "status": code})
}
