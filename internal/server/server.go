// Package server exposes the assembler over HTTP: span ingestion, trace
// queries, revision history and administrative flush/sweep endpoints.
package server

import (
	"encoding/json"
	"log"
	"net/http"
	"strconv"
	"strings"
	"time"

	"tracestitch/internal/assembler"
	"tracestitch/internal/model"
)

type Server struct {
	asm *assembler.Assembler
	mux *http.ServeMux
}

func New(asm *assembler.Assembler) *Server {
	s := &Server{asm: asm, mux: http.NewServeMux()}
	s.routes()
	return s
}

func (s *Server) Handler() http.Handler { return s.mux }

func (s *Server) routes() {
	s.mux.HandleFunc("POST /v1/spans", s.handleIngestBatch)
	s.mux.HandleFunc("POST /v1/span", s.handleIngestOne)
	s.mux.HandleFunc("GET /v1/traces", s.handleList)
	s.mux.HandleFunc("GET /v1/traces/{id}", s.handleGetTrace)
	s.mux.HandleFunc("GET /v1/traces/{id}/revisions", s.handleRevisions)
	s.mux.HandleFunc("GET /v1/traces/{id}/revisions/{v}", s.handleGetRevision)
	s.mux.HandleFunc("GET /v1/traces/{id}/containment", s.handleContainment)
	s.mux.HandleFunc("POST /admin/sweep", s.handleSweep)
	s.mux.HandleFunc("POST /admin/traces/{id}/flush", s.handleFlush)
	s.mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
}

// ---- ingestion ---------------------------------------------------------------

type ingestBatchRequest struct {
	Spans []model.Span `json:"spans"`
}

type ingestBatchResponse struct {
	Results []assembler.IngestResult `json:"results"`
}

func (s *Server) handleIngestBatch(w http.ResponseWriter, r *http.Request) {
	var req ingestBatchRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
		return
	}
	if len(req.Spans) == 0 {
		writeError(w, http.StatusBadRequest, "spans must not be empty")
		return
	}
	resp := ingestBatchResponse{Results: make([]assembler.IngestResult, 0, len(req.Spans))}
	anyAccepted := false
	for _, sp := range req.Spans {
		res := s.asm.Ingest(sp)
		resp.Results = append(resp.Results, res)
		if res.Status == "accepted" {
			anyAccepted = true
		}
	}
	// 207 semantics would be heavier; keep 200 unless every item was invalid.
	status := http.StatusOK
	if !anyAccepted {
		status = http.StatusBadRequest
	}
	writeJSON(w, status, resp)
}

func (s *Server) handleIngestOne(w http.ResponseWriter, r *http.Request) {
	var sp model.Span
	if err := json.NewDecoder(r.Body).Decode(&sp); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
		return
	}
	res := s.asm.Ingest(sp)
	status := http.StatusOK
	if res.Status == "invalid" || res.Status == "error" {
		status = http.StatusBadRequest
	}
	writeJSON(w, status, res)
}

// ---- queries -----------------------------------------------------------------

func (s *Server) handleList(w http.ResponseWriter, _ *http.Request) {
	ids := s.asm.ListTraces()
	views := make([]model.TraceView, 0, len(ids))
	for _, id := range ids {
		if v, ok := s.asm.GetTrace(id); ok {
			views = append(views, v)
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{"traces": views})
}

func (s *Server) handleGetTrace(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	v, ok := s.asm.GetTrace(id)
	if !ok {
		writeError(w, http.StatusNotFound, "trace not found: "+id)
		return
	}
	writeJSON(w, http.StatusOK, v)
}

func (s *Server) handleRevisions(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	v, ok := s.asm.GetTrace(id)
	if !ok {
		writeError(w, http.StatusNotFound, "trace not found: "+id)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"traceId":   id,
		"revisions": v.Revisions,
	})
}

func (s *Server) handleGetRevision(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	raw := r.PathValue("v")
	version, err := strconv.Atoi(raw)
	if err != nil || version < 1 {
		writeError(w, http.StatusBadRequest, "revision must be a positive integer: "+raw)
		return
	}
	rev, ok := s.asm.GetRevision(id, version)
	if !ok {
		writeError(w, http.StatusNotFound, "revision not found")
		return
	}
	writeJSON(w, http.StatusOK, rev)
}

func (s *Server) handleContainment(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if _, ok := s.asm.GetTrace(id); !ok {
		writeError(w, http.StatusNotFound, "trace not found: "+id)
		return
	}
	violation := s.asm.CheckRevisionContainment(id)
	resp := map[string]any{"traceId": id, "holds": violation == nil}
	if violation != nil {
		resp["violation"] = violation
	}
	writeJSON(w, http.StatusOK, resp)
}

// ---- admin -------------------------------------------------------------------

func (s *Server) handleSweep(w http.ResponseWriter, _ *http.Request) {
	n := s.asm.Sweep()
	writeJSON(w, http.StatusOK, map[string]any{"sealed": n})
}

func (s *Server) handleFlush(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if !s.asm.FlushTrace(id) {
		writeError(w, http.StatusConflict,
			"trace does not exist, is already sealed/complete, or has no revision: "+id)
		return
	}
	v, _ := s.asm.GetTrace(id)
	writeJSON(w, http.StatusOK, v)
}

// ---- helpers -----------------------------------------------------------------

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	if err := enc.Encode(body); err != nil {
		log.Printf("write response: %v", err)
	}
}

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]any{"error": strings.TrimSpace(msg)})
}

// StartSweeper runs Sweep on an interval until stopCh closes. The interval is
// the only real-waiting piece in the system; assembly itself stays clock-driven.
func StartSweeper(asm *assembler.Assembler, interval time.Duration, stopCh <-chan struct{}) {
	t := time.NewTicker(interval)
	defer t.Stop()
	for {
		select {
		case <-t.C:
			if n := asm.Sweep(); n > 0 {
				log.Printf("sweep sealed %d incomplete trace(s)", n)
			}
		case <-stopCh:
			return
		}
	}
}
