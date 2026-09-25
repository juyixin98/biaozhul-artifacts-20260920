// Package httpapi 暴露构建服务的 JSON HTTP 接口。
//
// 路由：
//
//	GET  /healthz                 存活探针
//	POST /v1/builds               发起一次确定性打包
//	GET  /v1/builds               列出构建（清单，不含响应内容）
//	GET  /v1/builds/{id}          查询单次构建
//
// 所有成功与失败响应均为 JSON：失败形如 {"error": {"code": "...", "message": "..."}}。
package httpapi

import (
	"encoding/json"
	"errors"
	"log"
	"net/http"

	"reproducible-archive/internal/archive"
	"reproducible-archive/internal/service"
)

// Server 持有路由与依赖。
type Server struct {
	mux *http.ServeMux
	mgr *service.Manager
}

// NewServer 构造 HTTP 服务。
func NewServer(mgr *service.Manager) *Server {
	s := &Server{
		mux: http.NewServeMux(),
		mgr: mgr,
	}
	s.mux.HandleFunc("GET /healthz", s.handleHealth)
	s.mux.HandleFunc("POST /v1/builds", s.handleCreateBuild)
	s.mux.HandleFunc("GET /v1/builds", s.handleListBuilds)
	s.mux.HandleFunc("GET /v1/builds/{id}", s.handleGetBuild)
	return s
}

// Handler 返回可挂载的根 handler。
func (s *Server) Handler() http.Handler { return s.mux }

func (s *Server) handleHealth(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *Server) handleCreateBuild(w http.ResponseWriter, r *http.Request) {
	var req service.BuildRequest
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20)).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid_request", "请求体不是合法 JSON: "+err.Error())
		return
	}
	mf, err := s.mgr.Build(req)
	if err == nil {
		writeJSON(w, http.StatusCreated, mf)
		return
	}

	// 错误分类 -> HTTP 状态码。
	var unsafeLink *archive.UnsafeSymlinkError
	switch {
	case errors.As(err, &unsafeLink):
		writeError(w, http.StatusUnprocessableEntity, "unsafe_symlink", err.Error())
	case errors.Is(err, service.ErrInvalidRequest):
		writeError(w, http.StatusBadRequest, "invalid_request", err.Error())
	default:
		log.Printf("构建失败: %v", err)
		writeError(w, http.StatusInternalServerError, "build_failed", err.Error())
	}
}

func (s *Server) handleListBuilds(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"builds": s.mgr.List()})
}

func (s *Server) handleGetBuild(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	mf, err := s.mgr.Get(id)
	if errors.Is(err, service.ErrNotFound) {
		writeError(w, http.StatusNotFound, "not_found", "构建 ID 不存在: "+id)
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, mf)
}

// errorBody 是统一的错误响应结构。
type errorBody struct {
	Error struct {
		Code    string `json:"code"`
		Message string `json:"message"`
	} `json:"error"`
}

func writeError(w http.ResponseWriter, status int, code, message string) {
	var b errorBody
	b.Error.Code = code
	b.Error.Message = message
	writeJSON(w, status, b)
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	if err := enc.Encode(v); err != nil {
		log.Printf("写 JSON 响应失败: %v", err)
	}
}
