// Package api exposes the analysis service over HTTP with chi.
package api

import (
	"encoding/json"
	"net/http"
	"strconv"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"

	"cresnap/internal/store"
	"cresnap/pkg/cganalyze"
	"cresnap/pkg/cgsample"
)

// Server wires the fixture root and the SQLite store to HTTP.
type Server struct {
	Root  string
	Store *store.Store
}

// NewRouter builds the chi mux.
func (s *Server) NewRouter() http.Handler {
	r := chi.NewRouter()
	r.Use(middleware.RealIP)
	r.Use(middleware.RequestID)
	r.Use(middleware.Recoverer)

	r.Get("/healthz", s.health)

	r.Route("/v1", func(r chi.Router) {
		r.Post("/ingest", s.ingest)
		r.Get("/ingest/runs", s.listRuns)
		r.Get("/containers", s.listContainers)
		r.Get("/containers/{container}", s.getContainer)
		r.Get("/containers/{container}/samples", s.listSamples)
		r.Get("/containers/{container}/samples/{ts}", s.getSample)
		r.Get("/containers/{container}/intervals", s.listIntervals)
		r.Get("/containers/{container}/intervals/{seq}", s.getInterval)
		r.Get("/events", s.listEvents)
	})
	return r
}

type errBody struct {
	Error ErrorDetail `json:"error"`
}

// ErrorDetail is the single error envelope.
type ErrorDetail struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

func writeError(w http.ResponseWriter, status int, code, msg string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(errBody{Error: ErrorDetail{Code: code, Message: msg}})
}

func writeJSON(w http.ResponseWriter, status int, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	enc.SetEscapeHTML(false)
	_ = enc.Encode(v)
}

func (s *Server) health(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok", "fixture_root": s.Root})
}

// resolveRun maps the optional run_id query param to a run id, defaulting to
// the newest run.
func (s *Server) resolveRun(w http.ResponseWriter, r *http.Request) (int64, bool) {
	q := r.URL.Query().Get("run_id")
	if q != "" {
		id, err := strconv.ParseInt(q, 10, 64)
		if err != nil || id < 1 {
			writeError(w, http.StatusBadRequest, "bad_run_id", "run_id must be a positive integer")
			return 0, false
		}
		exists, err := s.Store.RunExists(id)
		if err != nil {
			writeError(w, http.StatusInternalServerError, "db_error", err.Error())
			return 0, false
		}
		if !exists {
			writeError(w, http.StatusNotFound, "run_not_found", "no ingest run with id "+q)
			return 0, false
		}
		return id, true
	}
	id, err := s.Store.LatestRunID()
	if err != nil {
		writeError(w, http.StatusInternalServerError, "db_error", err.Error())
		return 0, false
	}
	if id == 0 {
		writeError(w, http.StatusConflict, "no_ingest", "no ingest has run yet; call POST /v1/ingest first")
		return 0, false
	}
	return id, true
}

// IngestResponse is returned by POST /v1/ingest.
type IngestResponse struct {
	Run            *store.RunSummary             `json:"run"`
	Containers     []ContainerIngestView         `json:"containers"`
	LoadErrors     []cgsample.LoadError          `json:"load_errors"`
}

// ContainerIngestView summarizes one container in an ingest response.
type ContainerIngestView struct {
	Container       string   `json:"container"`
	SampleCount     int      `json:"samples"`
	NominalSeconds  float64  `json:"nominal_seconds"`
	IntervalCount   int      `json:"intervals"`
	Events          []string `json:"event_kinds_in_order"`
}

func (s *Server) ingest(w http.ResponseWriter, r *http.Request) {
	loaded, err := cgsample.LoadRoot(s.Root)
	if err != nil {
		writeError(w, http.StatusBadRequest, "fixture_root_invalid", err.Error())
		return
	}
	rep := cganalyze.Analyze(s.Root, loaded.Containers, loaded.Errors)
	summary, err := s.Store.Ingest(s.Root, rep)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "db_error", err.Error())
		return
	}
	resp := IngestResponse{Run: summary, LoadErrors: rep.LoadErrors}
	for _, cr := range rep.Containers {
		v := ContainerIngestView{
			Container: cr.Container, SampleCount: cr.SampleCount,
			NominalSeconds: cr.NominalSeconds, IntervalCount: len(cr.Intervals),
		}
		for _, iv := range cr.Intervals {
			for _, ev := range iv.Events {
				v.Events = append(v.Events, ev.Kind)
			}
		}
		resp.Containers = append(resp.Containers, v)
	}
	writeJSON(w, http.StatusCreated, resp)
}

func (s *Server) listRuns(w http.ResponseWriter, r *http.Request) {
	runs, err := s.Store.ListRuns()
	if err != nil {
		writeError(w, http.StatusInternalServerError, "db_error", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]interface{}{"runs": runs})
}

func (s *Server) listContainers(w http.ResponseWriter, r *http.Request) {
	runID, ok := s.resolveRun(w, r)
	if !ok {
		return
	}
	cs, err := s.Store.ListContainers(runID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "db_error", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]interface{}{"run_id": runID, "containers": cs})
}

func (s *Server) getContainer(w http.ResponseWriter, r *http.Request) {
	runID, ok := s.resolveRun(w, r)
	if !ok {
		return
	}
	container := chi.URLParam(r, "container")
	exists, err := s.Store.ContainerExists(runID, container)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "db_error", err.Error())
		return
	}
	if !exists {
		writeError(w, http.StatusNotFound, "container_not_found", "container not found: "+container)
		return
	}
	samples, _ := s.Store.SampleList(runID, container)
	intervals, _ := s.Store.IntervalSeqList(runID, container)
	events, err := s.Store.QueryEvents(runID, container, "")
	if err != nil {
		writeError(w, http.StatusInternalServerError, "db_error", err.Error())
		return
	}
	kinds := []string{}
	for _, e := range events {
		kinds = append(kinds, e.Kind)
	}
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"run_id": runID, "container": container,
		"samples": samples, "intervals": intervals, "event_kinds": kinds,
	})
}

func (s *Server) listSamples(w http.ResponseWriter, r *http.Request) {
	runID, ok := s.resolveRun(w, r)
	if !ok {
		return
	}
	container := chi.URLParam(r, "container")
	ss, err := s.Store.SampleList(runID, container)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "db_error", err.Error())
		return
	}
	if ss == nil {
		writeError(w, http.StatusNotFound, "container_not_found", "container not found: "+container)
		return
	}
	writeJSON(w, http.StatusOK, map[string]interface{}{"run_id": runID, "container": container, "samples": ss})
}

func (s *Server) getSample(w http.ResponseWriter, r *http.Request) {
	runID, ok := s.resolveRun(w, r)
	if !ok {
		return
	}
	container := chi.URLParam(r, "container")
	ts := chi.URLParam(r, "ts")
	raw, err := s.Store.SampleJSON(runID, container, ts)
	if err != nil {
		// SampleJSON returns sql.ErrNoRows for both unknown container and ts.
		writeError(w, http.StatusNotFound, "sample_not_found",
			"sample not found for container "+container+" and timestamp "+ts)
		return
	}
	writeRawJSON(w, http.StatusOK, envelope(raw, runID, container))
}

func (s *Server) listIntervals(w http.ResponseWriter, r *http.Request) {
	runID, ok := s.resolveRun(w, r)
	if !ok {
		return
	}
	container := chi.URLParam(r, "container")
	ivs, err := s.Store.IntervalSeqList(runID, container)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "db_error", err.Error())
		return
	}
	if ivs == nil {
		// Distinguish unknown container.
		exists, _ := s.Store.ContainerExists(runID, container)
		if !exists {
			writeError(w, http.StatusNotFound, "container_not_found", "container not found: "+container)
			return
		}
	}
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"run_id": runID, "container": container,
		"intervals": ivs,
		"note":      "fetch full rate/event payloads via /intervals/{seq}",
	})
}

func (s *Server) getInterval(w http.ResponseWriter, r *http.Request) {
	runID, ok := s.resolveRun(w, r)
	if !ok {
		return
	}
	container := chi.URLParam(r, "container")
	seq, err := strconv.Atoi(chi.URLParam(r, "seq"))
	if err != nil || seq < 0 {
		writeError(w, http.StatusBadRequest, "bad_seq", "interval seq must be a non-negative integer")
		return
	}
	raw, err := s.Store.IntervalJSON(runID, container, seq)
	if err != nil {
		writeError(w, http.StatusNotFound, "interval_not_found",
			"interval not found for container "+container+" seq "+strconv.Itoa(seq))
		return
	}
	writeRawJSON(w, http.StatusOK, envelope(raw, runID, container))
}

// validEventKinds is enforced on the query so a typo cannot look like "none".
var validEventKinds = map[string]bool{
	cganalyze.EventOOMKill: true, cganalyze.EventOOMDetected: true,
	cganalyze.EventMemoryLimit: true, cganalyze.EventProcessExited: true,
	cganalyze.EventInstanceRebuilt: true,
}

func (s *Server) listEvents(w http.ResponseWriter, r *http.Request) {
	runID, ok := s.resolveRun(w, r)
	if !ok {
		return
	}
	container := r.URL.Query().Get("container")
	kind := r.URL.Query().Get("kind")
	if kind != "" && !validEventKinds[kind] {
		writeError(w, http.StatusBadRequest, "bad_kind", "unknown event kind: "+kind)
		return
	}
	rows, err := s.Store.QueryEvents(runID, container, kind)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "db_error", err.Error())
		return
	}
	out := make([]interface{}, 0, len(rows))
	for _, e := range rows {
		var ev, src interface{}
		_ = json.Unmarshal(e.Evidence, &ev)
		_ = json.Unmarshal(e.Sources, &src)
		out = append(out, map[string]interface{}{
			"container": e.Container, "seq": e.Seq, "kind": e.Kind,
			"at": e.At, "summary": e.Summary, "evidence": ev, "sources": src,
		})
	}
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"run_id": runID, "container_filter": container, "kind_filter": kind, "events": out,
	})
}

// envelope wraps a stored payload with run/container context.
func envelope(raw json.RawMessage, runID int64, container string) map[string]json.RawMessage {
	m := map[string]json.RawMessage{}
	_ = json.Unmarshal(raw, &m)
	rid, _ := json.Marshal(runID)
	cn, _ := json.Marshal(container)
	m["run_id"] = rid
	m["container_echo"] = cn
	return m
}

func writeRawJSON(w http.ResponseWriter, status int, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	enc.SetEscapeHTML(false)
	_ = enc.Encode(v)
}
