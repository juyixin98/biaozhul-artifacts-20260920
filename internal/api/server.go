// Package api exposes the analysis service over HTTP (Chi router).
package api

import (
	"encoding/json"
	"net/http"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"

	"cgroup-analyzer/internal/analysis"
	"cgroup-analyzer/internal/fixture"
	"cgroup-analyzer/internal/store"
)

type Server struct {
	st          *store.Store
	fixturesDir string
}

func NewServer(st *store.Store, fixturesDir string) *Server {
	return &Server{st: st, fixturesDir: fixturesDir}
}

func (s *Server) Router() http.Handler {
	r := chi.NewRouter()
	r.Use(middleware.RequestID)
	r.Use(middleware.Recoverer)
	r.Get("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	r.Route("/api", func(r chi.Router) {
		r.Post("/ingest", s.handleIngest)
		r.Get("/containers", s.handleListContainers)
		r.Get("/containers/{container}/instances/{instance}/report", s.handleReport)
		r.Get("/containers/{container}/instances/{instance}/rates", s.handleRates)
		r.Get("/containers/{container}/instances/{instance}/events", s.handleEvents)
	})
	return r
}

type ingestResponse struct {
	Instances int `json:"instances"`
	Samples   int `json:"samples"`
}

// handleIngest re-reads the fixture directory and atomically replaces the
// database contents.
func (s *Server) handleIngest(w http.ResponseWriter, r *http.Request) {
	insts, err := fixture.Load(s.fixturesDir)
	if err != nil {
		writeError(w, http.StatusBadRequest, "load fixtures: "+err.Error())
		return
	}
	if err := s.st.ReplaceInstances(insts); err != nil {
		writeError(w, http.StatusInternalServerError, "store fixtures: "+err.Error())
		return
	}
	n := 0
	for _, inst := range insts {
		n += len(inst.Samples)
	}
	writeJSON(w, http.StatusOK, ingestResponse{Instances: len(insts), Samples: n})
}

func (s *Server) handleListContainers(w http.ResponseWriter, _ *http.Request) {
	list, err := s.st.ListInstances()
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if list == nil {
		list = []store.InstanceSummary{}
	}
	writeJSON(w, http.StatusOK, map[string]any{"instances": list})
}

func (s *Server) analyze(w http.ResponseWriter, r *http.Request) (analysis.Report, bool) {
	container := chi.URLParam(r, "container")
	instance := chi.URLParam(r, "instance")
	inst, found, err := s.st.GetInstance(container, instance)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return analysis.Report{}, false
	}
	if !found {
		writeError(w, http.StatusNotFound, "instance not found: "+container+"/"+instance)
		return analysis.Report{}, false
	}
	return analysis.Analyze(inst), true
}

func (s *Server) handleReport(w http.ResponseWriter, r *http.Request) {
	if rep, ok := s.analyze(w, r); ok {
		writeJSON(w, http.StatusOK, rep)
	}
}

func (s *Server) handleRates(w http.ResponseWriter, r *http.Request) {
	rep, ok := s.analyze(w, r)
	if !ok {
		return
	}
	if rep.Intervals == nil {
		rep.Intervals = []analysis.IntervalRate{}
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"container": rep.Container, "instance": rep.Instance, "intervals": rep.Intervals,
	})
}

func (s *Server) handleEvents(w http.ResponseWriter, r *http.Request) {
	rep, ok := s.analyze(w, r)
	if !ok {
		return
	}
	if rep.Events == nil {
		rep.Events = []analysis.Event{}
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"container": rep.Container, "instance": rep.Instance, "events": rep.Events,
	})
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

func writeError(w http.ResponseWriter, code int, msg string) {
	writeJSON(w, code, map[string]string{"error": msg})
}
