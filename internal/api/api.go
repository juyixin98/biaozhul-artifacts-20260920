// Package api 提供差量更新服务的 HTTP JSON 接口（纯后端，无前端）。
package api

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"

	"deltaupdate/internal/patch"
	"deltaupdate/internal/service"
)

// Server 是 HTTP 接口层。
type Server struct {
	svc *service.Service
	mux *http.ServeMux
}

// New 构造接口层并注册路由。
func New(svc *service.Service) *Server {
	s := &Server{svc: svc, mux: http.NewServeMux()}
	s.mux.HandleFunc("POST /v1/projects", s.handleRegisterProject)
	s.mux.HandleFunc("GET /v1/projects", s.handleListProjects)
	s.mux.HandleFunc("GET /v1/projects/{name}", s.handleGetProject)
	s.mux.HandleFunc("POST /v1/projects/{name}/build", s.handleBuild)
	s.mux.HandleFunc("POST /v1/artifacts/{name}", s.handleIngest)
	s.mux.HandleFunc("GET /v1/artifacts/{name}/current", s.handleCurrent)
	s.mux.HandleFunc("GET /v1/artifacts/{name}/download", s.handleDownload)
	s.mux.HandleFunc("POST /v1/patches", s.handleMakePatch)
	s.mux.HandleFunc("POST /v1/patches/ingest", s.handleIngestPatch)
	s.mux.HandleFunc("GET /v1/patches/{digest}", s.handlePatchInfo)
	s.mux.HandleFunc("GET /v1/patches/{digest}/download", s.handlePatchDownload)
	s.mux.HandleFunc("POST /v1/apply", s.handleApply)
	return s
}

// Handler 返回根 http.Handler。
func (s *Server) Handler() http.Handler { return s.mux }

// ---- 响应封装 ----

type envelope struct {
	OK    bool           `json:"ok"`
	Data  any            `json:"data,omitempty"`
	Error *service.Error `json:"error,omitempty"`
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeOK(w http.ResponseWriter, data any) {
	writeJSON(w, http.StatusOK, envelope{OK: true, Data: data})
}

func writeErr(w http.ResponseWriter, err error) {
	var se *service.Error
	if errors.As(err, &se) {
		status := http.StatusBadRequest
		switch se.Code {
		case service.CodeNotFound:
			status = http.StatusNotFound
		case service.CodeDigestMismatch, service.CodeBlockMismatch, service.CodeCorruptPatch:
			status = http.StatusUnprocessableEntity
		case service.CodeNoSpace:
			status = http.StatusInsufficientStorage
		case service.CodeBuildFailed, service.CodeInternal:
			status = http.StatusInternalServerError
		}
		writeJSON(w, status, envelope{OK: false, Error: se})
		return
	}
	writeJSON(w, http.StatusInternalServerError, envelope{OK: false, Error: &service.Error{Code: service.CodeInternal, Msg: err.Error()}})
}

func decodeJSON(w http.ResponseWriter, r *http.Request, v any) bool {
	if err := json.NewDecoder(r.Body).Decode(v); err != nil {
		writeErr(w, &service.Error{Code: service.CodeBadRequest, Msg: "JSON 解析失败: " + err.Error()})
		return false
	}
	return true
}

// ---- 项目 ----

func (s *Server) handleRegisterProject(w http.ResponseWriter, r *http.Request) {
	var p service.Project
	if !decodeJSON(w, r, &p) {
		return
	}
	if err := s.svc.RegisterProject(p); err != nil {
		writeErr(w, err)
		return
	}
	writeOK(w, p)
}

func (s *Server) handleListProjects(w http.ResponseWriter, r *http.Request) {
	ps, err := s.svc.ListProjects()
	if err != nil {
		writeErr(w, err)
		return
	}
	writeOK(w, ps)
}

func (s *Server) handleGetProject(w http.ResponseWriter, r *http.Request) {
	p, err := s.svc.GetProject(r.PathValue("name"))
	if err != nil {
		writeErr(w, err)
		return
	}
	writeOK(w, p)
}

type buildRequest struct {
	Argv []string `json:"argv,omitempty"`
}

func (s *Server) handleBuild(w http.ResponseWriter, r *http.Request) {
	var req buildRequest
	if r.Body != nil && r.ContentLength != 0 {
		if !decodeJSON(w, r, &req) {
			return
		}
	}
	res, err := s.svc.Build(r.PathValue("name"), req.Argv)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeOK(w, res)
}

// ---- 制品 ----

func (s *Server) handleIngest(w http.ResponseWriter, r *http.Request) {
	res, err := s.svc.Ingest(r.PathValue("name"), r.Body)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeOK(w, res)
}

func (s *Server) handleCurrent(w http.ResponseWriter, r *http.Request) {
	d, err := s.svc.Current(r.PathValue("name"))
	if err != nil {
		writeErr(w, err)
		return
	}
	writeOK(w, map[string]string{"name": r.PathValue("name"), "digest": d})
}

func (s *Server) handleDownload(w http.ResponseWriter, r *http.Request) {
	dgst := r.URL.Query().Get("digest")
	if dgst == "" {
		var err error
		dgst, err = s.svc.Current(r.PathValue("name"))
		if err != nil {
			writeErr(w, err)
			return
		}
	}
	f, fi, err := s.svc.Store().OpenBlob(dgst)
	if err != nil {
		writeErr(w, err)
		return
	}
	defer f.Close()
	w.Header().Set("Content-Type", "application/octet-stream")
	w.Header().Set("X-Content-Digest", dgst)
	w.Header().Set("Content-Length", itoa(fi.Size()))
	_, _ = io.Copy(w, f)
}

// ---- 补丁 ----

type makePatchRequest struct {
	Name      string `json:"name"`
	OldDigest string `json:"old_digest,omitempty"`
	NewDigest string `json:"new_digest,omitempty"`
}

func (s *Server) handleMakePatch(w http.ResponseWriter, r *http.Request) {
	var req makePatchRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	res, err := s.svc.MakePatch(req.Name, req.OldDigest, req.NewDigest)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeOK(w, res)
}

func (s *Server) handleIngestPatch(w http.ResponseWriter, r *http.Request) {
	dgst, size, err := s.svc.Store().PutPatch(r.Body)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeOK(w, map[string]any{"digest": dgst, "size": size})
}

func (s *Server) handlePatchInfo(w http.ResponseWriter, r *http.Request) {
	f, _, err := s.svc.Store().OpenPatch(r.PathValue("digest"))
	if err != nil {
		writeErr(w, err)
		return
	}
	defer f.Close()
	pr, err := patch.NewReader(f)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeOK(w, pr.H)
}

func (s *Server) handlePatchDownload(w http.ResponseWriter, r *http.Request) {
	f, fi, err := s.svc.Store().OpenPatch(r.PathValue("digest"))
	if err != nil {
		writeErr(w, err)
		return
	}
	defer f.Close()
	w.Header().Set("Content-Type", "application/octet-stream")
	w.Header().Set("Content-Length", itoa(fi.Size()))
	_, _ = io.Copy(w, f)
}

// ---- 应用 ----

type applyRequest struct {
	Name        string `json:"name"`
	PatchDigest string `json:"patch_digest"`
}

func (s *Server) handleApply(w http.ResponseWriter, r *http.Request) {
	var req applyRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	if req.Name == "" || req.PatchDigest == "" {
		writeErr(w, &service.Error{Code: service.CodeBadRequest, Msg: "name 与 patch_digest 不能为空"})
		return
	}
	res, err := s.svc.ApplyPatch(req.Name, req.PatchDigest)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeOK(w, res)
}

func itoa(n int64) string {
	if n == 0 {
		return "0"
	}
	neg := n < 0
	if neg {
		n = -n
	}
	var buf [20]byte
	i := len(buf)
	for n > 0 {
		i--
		buf[i] = byte('0' + n%10)
		n /= 10
	}
	if neg {
		i--
		buf[i] = '-'
	}
	return string(buf[i:])
}
