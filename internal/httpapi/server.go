// Package httpapi 暴露 span 摄入与 trace 查询的 HTTP 接口。
package httpapi

import (
	"encoding/json"
	"errors"
	"log"
	"net/http"
	"strconv"
	"strings"

	"traceassembly/trace"
)

// Server 持有 HTTP 处理器及其依赖。
type Server struct {
	Asm *trace.Assembler
	Mux *http.ServeMux
}

// NewServer 装配路由。
func NewServer(asm *trace.Assembler) *Server {
	s := &Server{Asm: asm, Mux: http.NewServeMux()}
	s.Mux.HandleFunc("GET /healthz", s.health)
	s.Mux.HandleFunc("POST /v1/spans", s.ingest)
	s.Mux.HandleFunc("POST /v1/watermark", s.watermark)
	s.Mux.HandleFunc("GET /v1/traces", s.list)
	s.Mux.HandleFunc("GET /v1/traces/{id}", s.getTrace)
	s.Mux.HandleFunc("GET /v1/traces/{id}/revisions/{n}", s.getRevision)
	return s
}

type ingestRequest struct {
	Spans              []trace.Span `json:"spans"`
	AtLeastWatermarkNS int64        `json:"at_least_watermark_ns,omitempty"`
}

type ingestResponse struct {
	Results   []trace.IngestResult `json:"results"`
	Emitted   []trace.Revision     `json:"emitted_revisions"`
	Watermark int64                `json:"watermark_ns"`
}

type watermarkRequest struct {
	WatermarkNS int64 `json:"watermark_ns"`
}

func (s *Server) health(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *Server) ingest(w http.ResponseWriter, r *http.Request) {
	var req ingestRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
		return
	}
	if len(req.Spans) == 0 {
		writeError(w, http.StatusBadRequest, "spans must not be empty")
		return
	}
	results, emitted, err := s.Asm.IngestBatch(req.Spans, req.AtLeastWatermarkNS)
	if err != nil {
		if errors.Is(err, trace.ErrValidation) {
			writeError(w, http.StatusBadRequest, err.Error())
			return
		}
		log.Printf("ingest failed: %v", err)
		writeError(w, http.StatusInternalServerError, "internal error")
		return
	}
	writeJSON(w, http.StatusOK, ingestResponse{
		Results:   results,
		Emitted:   emitted,
		Watermark: s.Asm.Watermark(),
	})
}

func (s *Server) watermark(w http.ResponseWriter, r *http.Request) {
	var req watermarkRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
		return
	}
	emitted, err := s.Asm.AdvanceWatermark(req.WatermarkNS)
	if err != nil {
		log.Printf("watermark failed: %v", err)
		writeError(w, http.StatusInternalServerError, "internal error")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"watermark_ns":      s.Asm.Watermark(),
		"emitted_revisions": emitted,
	})
}

func (s *Server) list(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"watermark_ns": s.Asm.Watermark(),
		"traces":       s.Asm.ListTraces(),
	})
}

func (s *Server) getTrace(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	rev, ok, live := s.Asm.GetTrace(id)
	if !ok {
		writeError(w, http.StatusNotFound, "trace not found: "+id)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"live": live, "revision": rev})
}

func (s *Server) getRevision(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	n, err := strconv.ParseInt(r.PathValue("n"), 10, 64)
	if err != nil || n < 1 {
		writeError(w, http.StatusBadRequest, "revision number must be a positive integer")
		return
	}
	rev, ok := s.Asm.GetRevision(id, n)
	if !ok {
		writeError(w, http.StatusNotFound, "revision not found")
		return
	}
	writeJSON(w, http.StatusOK, rev)
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": strings.TrimSpace(msg)})
}
