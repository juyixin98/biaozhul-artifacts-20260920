// Package server 提供许可证判定的本地 JSON HTTP 接口。
// 不连接任何云平台；仅监听本机地址。
package server

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"sync"
	"time"

	"licensejudge/internal/cache"
	"licensejudge/internal/expression"
	"licensejudge/internal/judge"
	"licensejudge/internal/policy"
)

// Server 持有策略、引擎、缓存与工作目录。
type Server struct {
	policy  *policy.Policy
	engine  *judge.Engine
	cache   *cache.Cache
	workDir string
	logger  *log.Logger

	mu       sync.Mutex
	auditLog *os.File
}

// Config 是服务构造参数。
type Config struct {
	Policy  *policy.Policy
	Cache   *cache.Cache
	WorkDir string
	Logger  *log.Logger
}

// New 创建服务实例并确保工作目录存在。
func New(cfg Config) (*Server, error) {
	if cfg.Policy == nil {
		return nil, fmt.Errorf("必须提供策略")
	}
	if cfg.Cache == nil {
		return nil, fmt.Errorf("必须提供缓存")
	}
	if cfg.WorkDir == "" {
		return nil, fmt.Errorf("必须提供工作目录")
	}
	if err := os.MkdirAll(cfg.WorkDir, 0o755); err != nil {
		return nil, fmt.Errorf("创建工作目录失败: %w", err)
	}
	// 防护：缓存目录不得位于工作目录之下，反之工作目录也不位于缓存目录之下。
	if err := assertSeparate(cfg.Cache.Dir(), cfg.WorkDir); err != nil {
		return nil, err
	}
	logger := cfg.Logger
	if logger == nil {
		logger = log.New(os.Stderr, "[licensejudge] ", log.LstdFlags)
	}
	s := &Server{
		policy:  cfg.Policy,
		engine:  judge.NewEngine(cfg.Policy),
		cache:   cfg.Cache,
		workDir: cfg.WorkDir,
		logger:  logger,
	}
	f, err := os.OpenFile(filepath.Join(cfg.WorkDir, "audit.log"),
		os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		return nil, fmt.Errorf("打开审计日志失败: %w", err)
	}
	s.auditLog = f
	return s, nil
}

// Close 释放服务持有的文件句柄。
func (s *Server) Close() error {
	if s.auditLog != nil {
		return s.auditLog.Close()
	}
	return nil
}

// Routes 注册 HTTP 路由。
func (s *Server) Routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", s.handleHealth)
	mux.HandleFunc("/v1/policies", s.handlePolicy)
	mux.HandleFunc("/v1/evaluate", s.handleEvaluate)
	return mux
}

// EvaluateRequest 是 POST /v1/evaluate 的请求体。
type EvaluateRequest struct {
	Expression string `json:"expression"`
}

// ErrorResponse 是统一的错误响应。
type ErrorResponse struct {
	Error      string `json:"error"`
	Code       string `json:"code"`
	Disclaimer string `json:"disclaimer"`
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *Server) handlePolicy(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "method_not_allowed", "仅支持 GET")
		return
	}
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	ids := make([]string, 0, len(s.policy.Licenses))
	for id := range s.policy.Licenses {
		ids = append(ids, id)
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"name":     s.policy.Name,
		"version":  s.policy.Version,
		"unlisted": s.policy.Unlisted,
		"licenses": ids,
	})
}

func (s *Server) handleEvaluate(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "method_not_allowed", "仅支持 POST")
		return
	}
	var req EvaluateRequest
	dec := json.NewDecoder(r.Body)
	if err := dec.Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid_json", "请求体不是合法 JSON: "+err.Error())
		return
	}
	if req.Expression == "" {
		writeError(w, http.StatusBadRequest, "missing_expression", "字段 expression 不能为空")
		return
	}

	jobID := newJobID()
	cached := false
	node, err := s.cache.Get(req.Expression)
	if err == nil {
		cached = true
	} else {
		node, err = expression.Parse(req.Expression)
		if err != nil {
			s.audit(jobID, req.Expression, "parse_error", err.Error())
			writeError(w, http.StatusBadRequest, "parse_error", err.Error())
			return
		}
		if putErr := s.cache.Put(req.Expression, node); putErr != nil {
			s.logger.Printf("警告: 写入 AST 缓存失败（不影响判定）: %v", putErr)
		}
	}

	result := s.engine.EvalAST(node)
	s.audit(jobID, req.Expression, string(result.Verdict), expression.String(node))

	// 每个作业在工作目录中落一份判定结果（缓存与工作目录分离的体现）。
	jobFile := filepath.Join(s.workDir, jobID+".json")
	jobPayload, _ := json.MarshalIndent(map[string]any{
		"job_id":     jobID,
		"expression": req.Expression,
		"cached":     cached,
		"result":     result,
	}, "", "  ")
	if werr := os.WriteFile(jobFile, jobPayload, 0o644); werr != nil {
		s.logger.Printf("警告: 写入作业文件失败（不影响响应）: %v", werr)
	}

	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	writeJSON(w, http.StatusOK, map[string]any{
		"job_id":     jobID,
		"expression": req.Expression,
		"cached":     cached,
		"result":     result,
	})
}

func (s *Server) audit(jobID, expr, verdict, detail string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	line := fmt.Sprintf("%s job=%s verdict=%s expr=%q detail=%q\n",
		time.Now().UTC().Format(time.RFC3339), jobID, verdict, expr, detail)
	if s.auditLog != nil {
		if _, err := s.auditLog.WriteString(line); err != nil {
			s.logger.Printf("警告: 写审计日志失败: %v", err)
		}
	}
}

func newJobID() string {
	var b [8]byte
	_, _ = rand.Read(b[:])
	return "job-" + hex.EncodeToString(b[:])
}

// IsParseError 供 CLI 区分退出码。
func IsParseError(err error) bool {
	var pe *judge.ParseError
	return errors.As(err, &pe)
}

func writeError(w http.ResponseWriter, status int, code, msg string) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(ErrorResponse{
		Error:      msg,
		Code:       code,
		Disclaimer: judge.Disclaimer,
	})
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(v); err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
	}
}

// assertSeparate 检查两个目录是否互不为前缀，避免缓存与工作数据互混。
func assertSeparate(cacheDir, workDir string) error {
	cAbs, err1 := filepath.Abs(cacheDir)
	wAbs, err2 := filepath.Abs(workDir)
	if err1 != nil || err2 != nil {
		return fmt.Errorf("无法解析目录绝对路径")
	}
	if cAbs == wAbs || isWithin(cAbs, wAbs) || isWithin(wAbs, cAbs) {
		return fmt.Errorf("缓存目录 (%s) 与工作目录 (%s) 必须分离，不得互为子目录", cAbs, wAbs)
	}
	return nil
}

func isWithin(child, parent string) bool {
	rel, err := filepath.Rel(parent, child)
	if err != nil {
		return false
	}
	return rel != "." && rel != ".." && !filepath.IsAbs(rel) &&
		len(rel) >= 3 && rel[:3] != ".."+string(filepath.Separator)
}
