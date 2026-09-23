// Package server 提供本地构建工程服务的 JSON HTTP 接口。
// 仅监听本机回环地址，不连接任何云平台。
package server

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"

	"cdbg/internal/engine"
	"cdbg/internal/graph"
)

// Server 持有引擎与默认目录配置。
type Server struct {
	eng            *engine.Engine
	defaultWorkDir string
	defaultCacheD  string
	stateDir       string
}

// New 创建 HTTP 服务。
func New(eng *engine.Engine, defaultWorkDir, defaultCacheDir, stateDir string) *Server {
	return &Server{
		eng:            eng,
		defaultWorkDir: defaultWorkDir,
		defaultCacheD:  defaultCacheDir,
		stateDir:       stateDir,
	}
}

// Routes 注册路由并返回 http.Handler。
func (s *Server) Routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", s.handleHealth)
	mux.HandleFunc("/api/v1/build", s.handleBuild)
	mux.HandleFunc("/api/v1/explain", s.handleExplain)
	mux.HandleFunc("/api/v1/cache/entries", s.handleCacheList)
	mux.HandleFunc("/api/v1/cache/entries/", s.handleCacheEntry)
	return mux
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "only GET")
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

// BuildRequest 是 POST /api/v1/build 的请求体。
type BuildRequest struct {
	WorkDir  string      `json:"work_dir"`
	SpecFile string      `json:"spec_file,omitempty"` // 可选：从文件读取 spec
	Spec     *graph.Spec `json:"spec,omitempty"`
	Targets  []string    `json:"targets,omitempty"`
	Force    bool        `json:"force,omitempty"`
	DryRun   bool        `json:"dry_run,omitempty"`
}

func (s *Server) handleBuild(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "only POST")
		return
	}
	req, err := decodeBuildRequest(r)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	workdir := req.WorkDir
	if workdir == "" {
		workdir = s.defaultWorkDir
	}
	workdir, err = filepath.Abs(workdir)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	if err := os.MkdirAll(workdir, 0o755); err != nil {
		writeError(w, http.StatusBadRequest, "cannot create work_dir: "+err.Error())
		return
	}
	if err := validateDirsSeparate(workdir, s.defaultCacheD, s.stateDir); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	result, err := s.eng.Build(workdir, engine.Options{
		Spec:    req.Spec,
		Targets: req.Targets,
		Force:   req.Force,
		DryRun:  req.DryRun,
	})
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	// 规格错误与依赖环属于请求问题：400；节点构建失败属于执行结果：200 + success=false。
	status := http.StatusOK
	if result.Error != "" && (len(result.Cycle) > 0 || isSpecError(result.Error)) {
		status = http.StatusBadRequest
	}
	writeJSON(w, status, result)
}

func (s *Server) handleExplain(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "only POST")
		return
	}
	req, err := decodeBuildRequest(r)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	req.DryRun = true // explain 永不执行
	workdir := req.WorkDir
	if workdir == "" {
		workdir = s.defaultWorkDir
	}
	workdir, _ = filepath.Abs(workdir)
	if err := os.MkdirAll(workdir, 0o755); err != nil {
		writeError(w, http.StatusBadRequest, "cannot create work_dir: "+err.Error())
		return
	}
	result, err := s.eng.Build(workdir, engine.Options{
		Spec:    req.Spec,
		Targets: req.Targets,
		DryRun:  true,
	})
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	status := http.StatusOK
	if result.Error != "" && (len(result.Cycle) > 0 || isSpecError(result.Error)) {
		status = http.StatusBadRequest
	}
	writeJSON(w, status, result)
}

func (s *Server) handleCacheList(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "only GET")
		return
	}
	entries, err := s.eng.CacheEntries()
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"cache_dir": s.defaultCacheD, "entries": entries})
}

func (s *Server) handleCacheEntry(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "only GET")
		return
	}
	// 路径形如 /api/v1/cache/entries/<sha256>
	key := strings.TrimPrefix(r.URL.Path, "/api/v1/cache/entries/")
	if key == "" || strings.ContainsAny(key, "/\\") || len(key) != 64 {
		writeError(w, http.StatusBadRequest, "entry key must be a 64-char SHA-256 hex")
		return
	}
	meta, err := s.eng.CacheEntry(key)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if meta == nil {
		writeError(w, http.StatusNotFound, "cache entry not found")
		return
	}
	writeJSON(w, http.StatusOK, meta)
}

func decodeBuildRequest(r *http.Request) (*BuildRequest, error) {
	body, err := io.ReadAll(io.LimitReader(r.Body, 8<<20))
	if err != nil {
		return nil, err
	}
	var req BuildRequest
	if len(body) > 0 {
		if err := json.Unmarshal(body, &req); err != nil {
			return nil, fmt.Errorf("invalid JSON: %w", err)
		}
	}
	if req.Spec == nil && req.SpecFile != "" {
		raw, err := os.ReadFile(req.SpecFile)
		if err != nil {
			return nil, fmt.Errorf("read spec_file: %w", err)
		}
		var sp graph.Spec
		if err := json.Unmarshal(raw, &sp); err != nil {
			return nil, fmt.Errorf("parse spec_file: %w", err)
		}
		req.Spec = &sp
	}
	if req.Spec == nil {
		return nil, fmt.Errorf("request requires either spec or spec_file")
	}
	return &req, nil
}

func validateDirsSeparate(workdir, cacheDir, stateDir string) error {
	if isWithin(cacheDir, workdir) {
		return fmt.Errorf("cache_dir (%s) must not be inside work_dir (%s)", cacheDir, workdir)
	}
	if isWithin(stateDir, workdir) {
		return fmt.Errorf("state_dir (%s) must not be inside work_dir (%s)", stateDir, workdir)
	}
	return nil
}

// isWithin reports whether child == parent 或 child 位于 parent 之内。
func isWithin(child, parent string) bool {
	rel, err := filepath.Rel(parent, child)
	if err != nil {
		return false
	}
	rel = filepath.ToSlash(rel)
	return rel == "." || (rel != ".." && !strings.HasPrefix(rel, "../"))
}

func isSpecError(msg string) bool {
	return strings.Contains(msg, "spec requires") ||
		strings.Contains(msg, "unknown") ||
		strings.Contains(msg, "invalid template") ||
		strings.Contains(msg, "undefined param") ||
		strings.Contains(msg, "duplicate node") ||
		strings.Contains(msg, "must be relative") ||
		strings.Contains(msg, "escapes the work directory")
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]any{"success": false, "error": msg})
}
