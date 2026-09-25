// Package service 提供纯后端的 JSON 接口：依赖求解与本地构建规划。
// 所有数据都来自请求体或本地夹具，服务自身不访问网络、不下载包。
package service

import (
	"encoding/json"
	"fmt"
	"net/http"

	"depsolver/internal/model"
	"depsolver/internal/registry"
	"depsolver/internal/solver"
)

// ResolveRequest 是 POST /v1/resolve 的请求体。
type ResolveRequest struct {
	Roots              []model.Dependency  `json:"roots"`
	Registry           model.RegistryInput `json:"registry"`
	IncludePrereleases bool                `json:"includePrereleases,omitempty"`
}

// PlanRequest 是 POST /v1/build/plan 的请求体。
// WorkspaceDir 与 CacheDir 分离：锁文件落在工作目录，解析过程产生的缓存元数据落在缓存目录。
type PlanRequest struct {
	ResolveRequest
	WorkspaceDir string `json:"workspaceDir"`
	CacheDir     string `json:"cacheDir"`
	LockfileName string `json:"lockfileName,omitempty"`
}

// APIError 是统一错误体。
type APIError struct {
	Error ErrorBody `json:"error"`
}

// ErrorBody 是错误详情。
type ErrorBody struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

// Handler 装配 HTTP 路由。
type Handler struct {
	// PlanWriter 执行可选的本地落盘（工作目录锁文件 + 缓存标记）；
	// 为 nil 时 /v1/build/plan 只返回内容而不写盘。
	PlanWriter PlanWriter
}

// NewHandler 创建默认 Handler。
func NewHandler() *Handler { return &Handler{} }

// Routes 在给定 mux 上注册路由并返回。
func (h *Handler) Routes(mux *http.ServeMux) *http.ServeMux {
	mux.HandleFunc("GET /healthz", h.health)
	mux.HandleFunc("POST /v1/resolve", h.resolve)
	mux.HandleFunc("POST /v1/build/plan", h.plan)
	return mux
}

func (h *Handler) health(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (h *Handler) resolve(w http.ResponseWriter, r *http.Request) {
	var req ResolveRequest
	if err := decodeStrict(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, "bad-request", err.Error())
		return
	}
	res, status, err := runResolve(req)
	if err != nil {
		writeError(w, http.StatusBadRequest, "bad-request", err.Error())
		return
	}
	writeJSON(w, status, res)
}

func (h *Handler) plan(w http.ResponseWriter, r *http.Request) {
	var req PlanRequest
	if err := decodeStrict(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, "bad-request", err.Error())
		return
	}
	res, status, err := runResolve(req.ResolveRequest)
	if err != nil {
		writeError(w, http.StatusBadRequest, "bad-request", err.Error())
		return
	}
	if !res.Satisfiable {
		// 无解不产生构建计划
		writeJSON(w, status, map[string]any{"resolve": res})
		return
	}
	lockfile := BuildLockfile(req.Roots, res)
	out := PlanOutput{Resolve: res, Lockfile: lockfile}
	if h.PlanWriter != nil {
		written, err := h.PlanWriter.Write(req, lockfile)
		if err != nil {
			writeError(w, http.StatusBadRequest, "plan-write-failed", err.Error())
			return
		}
		out.Written = written
	}
	writeJSON(w, http.StatusOK, out)
}

func runResolve(req ResolveRequest) (*solver.Result, int, error) {
	if len(req.Roots) == 0 {
		return nil, 0, fmt.Errorf("roots must contain at least one dependency")
	}
	rootNames := map[string]struct{}{}
	for _, d := range req.Roots {
		if !registry.ValidName(d.Name) {
			return nil, 0, fmt.Errorf("invalid root dependency name %q", d.Name)
		}
		if _, dup := rootNames[d.Name]; dup {
			return nil, 0, fmt.Errorf("duplicate root dependency %q", d.Name)
		}
		rootNames[d.Name] = struct{}{}
	}
	reg, err := registry.Build(req.Registry)
	if err != nil {
		return nil, 0, err
	}
	res, err := solver.Solve(reg, req.Roots, solver.Options{IncludePrereleases: req.IncludePrereleases})
	if err != nil {
		return nil, 0, err
	}
	if !res.Satisfiable {
		return res, http.StatusConflict, nil
	}
	return res, http.StatusOK, nil
}

// Lockfile 是确定性锁文件（字段排序固定，不含时间戳）。
type Lockfile struct {
	Schema   string          `json:"schema"`
	Roots    []string        `json:"roots"`
	Packages []LockedPackage `json:"packages"`
	Cycles   []solver.Cycle  `json:"cycles,omitempty"`
}

// LockedPackage 是锁文件中的一条版本钉选。
type LockedPackage struct {
	Name    string `json:"name"`
	Version string `json:"version"`
}

// PlanOutput 是 /v1/build/plan 成功响应。
type PlanOutput struct {
	Resolve  *solver.Result `json:"resolve"`
	Lockfile Lockfile       `json:"lockfile"`
	Written  *WrittenFiles  `json:"written,omitempty"`
}

// WrittenFiles 记录落盘位置（仅本地，不触发任何下载）。
type WrittenFiles struct {
	LockfilePath string `json:"lockfilePath,omitempty"`
	CacheRecord  string `json:"cacheRecord,omitempty"`
}

// BuildLockfile 由求解结果构造确定性锁文件。
func BuildLockfile(roots []model.Dependency, res *solver.Result) Lockfile {
	lf := Lockfile{Schema: "depsolver.lock/v1", Cycles: res.Cycles}
	for _, d := range roots {
		lf.Roots = append(lf.Roots, d.Name)
	}
	for _, sel := range res.Selections {
		lf.Packages = append(lf.Packages, LockedPackage{Name: sel.Package, Version: sel.Version})
	}
	return lf
}

func decodeStrict(r *http.Request, v any) error {
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		return fmt.Errorf("invalid JSON body: %w", err)
	}
	return nil
}

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(body)
}

func writeError(w http.ResponseWriter, status int, code, msg string) {
	writeJSON(w, status, APIError{Error: ErrorBody{Code: code, Message: msg}})
}
