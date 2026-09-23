// Package api 提供 HTTP 摄入、查询与样例生成接口。
package api

import (
	"encoding/json"
	"net/http"
	"strconv"
	"strings"

	"criticalpath/internal/analyzer"
	"criticalpath/internal/store"
)

type Server struct {
	Store *store.Store
	Mux   *http.ServeMux
}

func NewServer(st *store.Store) *Server {
	s := &Server{Store: st, Mux: http.NewServeMux()}
	s.routes()
	return s
}

func (s *Server) routes() {
	s.Mux.HandleFunc("GET /healthz", s.health)
	s.Mux.HandleFunc("GET /v1/traces", s.listTraces)
	s.Mux.HandleFunc("POST /v1/traces/{tid}/spans", s.ingest)
	s.Mux.HandleFunc("GET /v1/traces/{tid}", s.getTrace)
	s.Mux.HandleFunc("GET /v1/traces/{tid}/critical-path", s.criticalPath)
	s.Mux.HandleFunc("POST /v1/sample/{kind}", s.createSample)
}

func (s *Server) health(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"status": "ok"})
}

type ingestReq struct {
	Spans []analyzer.Span `json:"spans"`
}

func (s *Server) ingest(w http.ResponseWriter, r *http.Request) {
	tid := r.PathValue("tid")
	var req ingestReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, "请求体不是合法 JSON: "+err.Error())
		return
	}
	if len(req.Spans) == 0 {
		writeErr(w, http.StatusBadRequest, "spans 不能为空")
		return
	}
	for i := range req.Spans {
		sp := &req.Spans[i]
		if sp.TraceID == "" {
			sp.TraceID = tid
		}
		if sp.TraceID != tid {
			writeErr(w, http.StatusBadRequest,
				"span["+strconv.Itoa(i)+"].trace_id="+sp.TraceID+" 与路径 trace_id="+tid+" 不一致")
			return
		}
		if sp.SpanID == "" || sp.Name == "" {
			writeErr(w, http.StatusBadRequest,
				"span["+strconv.Itoa(i)+"] 缺少 span_id 或 name")
			return
		}
	}
	if _, err := s.Store.PutBatch(req.Spans); err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	// 摄入成功后立即返回分析报告；结构性错误（循环等）数据已落库，但以 422 提示。
	rep := analyzer.Analyze(tid, req.Spans)
	status := http.StatusOK
	if rep.HasError {
		status = http.StatusUnprocessableEntity
	}
	writeJSON(w, status, map[string]any{
		"ingested": len(req.Spans),
		"report":   rep,
		"note":     "完整报告以 GET /v1/traces/" + tid + "/critical-path 为准（含该 trace 全部 span）",
	})
}

func (s *Server) getTrace(w http.ResponseWriter, r *http.Request) {
	tid := r.PathValue("tid")
	spans, ok := s.Store.GetTrace(tid)
	if !ok {
		writeErr(w, http.StatusNotFound, "trace 不存在: "+tid)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"trace_id": tid, "span_count": len(spans), "spans": spans})
}

func (s *Server) criticalPath(w http.ResponseWriter, r *http.Request) {
	tid := r.PathValue("tid")
	spans, ok := s.Store.GetTrace(tid)
	if !ok {
		writeErr(w, http.StatusNotFound, "trace 不存在: "+tid)
		return
	}
	rep := analyzer.Analyze(tid, spans)
	status := http.StatusOK
	if rep.HasError {
		status = http.StatusUnprocessableEntity
	}
	writeJSON(w, status, rep)
}

func (s *Server) listTraces(w http.ResponseWriter, r *http.Request) {
	ids := s.Store.TraceIDs()
	type item struct {
		TraceID   string `json:"trace_id"`
		SpanCount int    `json:"span_count"`
	}
	items := make([]item, 0, len(ids))
	for _, id := range ids {
		if sp, ok := s.Store.GetTrace(id); ok {
			items = append(items, item{TraceID: id, SpanCount: len(sp)})
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{"traces": items})
}

// createSample 生成内置合成 trace 并落库。kind 见 analyzer 中的构造函数。
// 查询参数 trace_id 可覆盖默认 id，synth 可用 n 指定分片数。
func (s *Server) createSample(w http.ResponseWriter, r *http.Request) {
	kind := r.PathValue("kind")
	tid := r.URL.Query().Get("trace_id")
	var spans []analyzer.Span
	switch kind {
	case "demo":
		spans = analyzer.DemoSpans(tid)
	case "overlap":
		spans = analyzer.OverlappingSpans(tid)
	case "cycle":
		spans = analyzer.CycleSpans(tid)
	case "missing":
		spans = analyzer.MissingParentSpans(tid)
	case "selfoverlap":
		spans = analyzer.NestedOverlapSpans(tid)
	case "synth":
		n := 4
		if v := r.URL.Query().Get("n"); v != "" {
			x, err := strconv.Atoi(v)
			if err != nil || x < 1 || x > 1000 {
				writeErr(w, http.StatusBadRequest, "n 必须是 1-1000 的整数")
				return
			}
			n = x
		}
		spans = analyzer.RandomishSpans(tid, n)
	default:
		writeErr(w, http.StatusBadRequest,
			"未知 kind="+kind+"，可选: demo overlap cycle missing selfoverlap synth")
		return
	}
	if _, err := s.Store.PutBatch(spans); err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	tid = spans[0].TraceID
	all, _ := s.Store.GetTrace(tid)
	rep := analyzer.Analyze(tid, all)
	status := http.StatusOK
	if rep.HasError {
		status = http.StatusUnprocessableEntity
	}
	writeJSON(w, status, map[string]any{"kind": kind, "report": rep, "spans": spans})
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

func writeErr(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]any{"error": strings.TrimSpace(msg)})
}
