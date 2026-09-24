// Package api 提供镜像离线准入的纯后端 HTTP 接口（Chi 路由）。
package api

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strconv"

	"github.com/go-chi/chi/v5"

	"mirrorsec/internal/evaluate"
	"mirrorsec/internal/model"
	"mirrorsec/internal/store"
)

type Server struct {
	engine *evaluate.Engine
	store  *store.Store
}

func NewServer(engine *evaluate.Engine, st *store.Store) *Server {
	return &Server{engine: engine, store: st}
}

func (s *Server) Router() http.Handler {
	r := chi.NewRouter()
	r.Get("/healthz", s.health)
	r.Get("/v1/policy", s.policyInfo)
	r.Post("/v1/admission/evaluate", s.evaluate)
	r.Get("/v1/reports", s.listReports)
	r.Get("/v1/reports/{id}", s.getReport)
	r.NotFound(func(w http.ResponseWriter, _ *http.Request) {
		writeError(w, http.StatusNotFound, "NOT_FOUND", "路径不存在")
	})
	r.MethodNotAllowed(func(w http.ResponseWriter, _ *http.Request) {
		writeError(w, http.StatusMethodNotAllowed, "METHOD_NOT_ALLOWED", "方法不允许")
	})
	return r
}

func (s *Server) health(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"status": "ok"})
}

func (s *Server) policyInfo(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, model.PolicyInfo{
		Version:   s.engine.Version(),
		Hash:      s.engine.Hash(),
		Allowlist: s.engine.Allowlist(),
	})
}

func (s *Server) evaluate(w http.ResponseWriter, r *http.Request) {
	// 限制请求体大小，防止异常大输入拖垮离线服务。
	r.Body = http.MaxBytesReader(w, r.Body, 8<<20)
	body, err := io.ReadAll(r.Body)
	if err != nil {
		writeError(w, http.StatusBadRequest, "BAD_REQUEST", "读取请求体失败: "+err.Error())
		return
	}
	var req model.AdmissionRequest
	if err := json.Unmarshal(body, &req); err != nil {
		writeError(w, http.StatusBadRequest, "BAD_REQUEST", "请求不是合法 JSON: "+err.Error())
		return
	}
	report, err := s.engine.Evaluate(r.Context(), req)
	if err != nil {
		// 镜像缺失/非法等 fail-closed 情形返回 422，语义上是“无法评估”，不是服务器崩溃。
		writeError(w, http.StatusUnprocessableEntity, "EVALUATION_FAILED", err.Error())
		return
	}
	if err := s.store.Save(report); err != nil {
		writeError(w, http.StatusInternalServerError, "STORE_ERROR", err.Error())
		return
	}
	// HTTP 层始终 200，准入结论以 body.decision 为准（admission webhook 惯例）。
	writeJSON(w, http.StatusOK, report)
}

func (s *Server) listReports(w http.ResponseWriter, r *http.Request) {
	limit := 50
	if v := r.URL.Query().Get("limit"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n <= 0 || n > 500 {
			writeError(w, http.StatusBadRequest, "BAD_REQUEST", "limit 必须是 1..500 的整数")
			return
		}
		limit = n
	}
	items, err := s.store.List(limit)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "STORE_ERROR", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"reports": items, "count": len(items)})
}

func (s *Server) getReport(w http.ResponseWriter, r *http.Request) {
	id := chi.URLParam(r, "id")
	rep, err := s.store.Get(id)
	if errors.Is(err, store.ErrNotFound) {
		writeError(w, http.StatusNotFound, "NOT_FOUND", "报告不存在: "+id)
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, "STORE_ERROR", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, rep)
}

type errorBody struct {
	Error struct {
		Code    string `json:"code"`
		Message string `json:"message"`
	} `json:"error"`
}

func writeError(w http.ResponseWriter, status int, code, msg string) {
	var b errorBody
	b.Error.Code = code
	b.Error.Message = msg
	writeJSON(w, status, b)
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}
