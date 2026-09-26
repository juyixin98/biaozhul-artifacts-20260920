// Package api 装配本地 HTTP 服务：业务幂等接口、观测/控制面，以及本进程假外部系统。
package api

import (
	"encoding/json"
	"io"
	"net/http"
	"time"

	"idempotentsave/internal/clock"
	"idempotentsave/internal/service"
	"idempotentsave/internal/store"
)

// Config 是服务装配参数。
type Config struct {
	Store        *store.Store
	Service      *service.Service
	AuditBaseURL string // 外部假审计系统地址（独立进程），用于观测/复位
	Clock        clock.Clock
	WaitMax      time.Duration // 处理中重复请求最多等待多久
	RealClock    bool          // 管理面是否允许拨钟（仅假时钟时允许）
}

// Server 持有路由与依赖。
type Server struct {
	cfg  Config
	mux  *http.ServeMux
	http *http.Client
}

// New 创建业务 API。
func New(cfg Config) *Server {
	if cfg.WaitMax <= 0 {
		cfg.WaitMax = 5 * time.Second
	}
	s := &Server{
		cfg:  cfg,
		mux:  http.NewServeMux(),
		http: &http.Client{Timeout: 3 * time.Second},
	}
	s.routes()
	return s
}

func (s *Server) routes() {
	s.mux.HandleFunc("/v1/deposits", s.handleDeposit)
	s.mux.HandleFunc("/v1/idempotency/", s.handleLookup)
	s.mux.HandleFunc("/admin/ledger", s.handleLedger)
	s.mux.HandleFunc("/admin/stats", s.handleStats)
	s.mux.HandleFunc("/admin/reset", s.handleReset)
	s.mux.HandleFunc("/admin/clock/advance", s.handleClockAdvance)
	s.mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
}

// Handler 返回根处理器。
func (s *Server) Handler() http.Handler {
	return logRequests(s.mux)
}

func (s *Server) handleLookup(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "method_not_allowed", "use GET")
		return
	}
	key := r.URL.Path[len("/v1/idempotency/"):]
	if key == "" {
		writeError(w, http.StatusBadRequest, "missing_key", "idempotency key is required")
		return
	}
	rec, ok := s.cfg.Store.Lookup(key)
	if !ok {
		writeError(w, http.StatusNotFound, "unknown_key", "no record for this idempotency key")
		return
	}
	writeJSON(w, http.StatusOK, recordView(rec))
}

func (s *Server) handleLedger(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodGet:
		writeJSON(w, http.StatusOK, map[string]any{
			"effects":  s.cfg.Store.Ledger(),
			"balances": s.cfg.Store.Stats().Balances,
		})
	case http.MethodDelete:
		if err := s.cfg.Store.Reset(); err != nil {
			writeError(w, http.StatusInternalServerError, "reset_failed", err.Error())
			return
		}
		writeJSON(w, http.StatusOK, map[string]string{"status": "reset"})
	default:
		writeError(w, http.StatusMethodNotAllowed, "method_not_allowed", "use GET or DELETE")
	}
}

func (s *Server) handleStats(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "method_not_allowed", "use GET")
		return
	}
	st := s.cfg.Store.Stats()
	auditCalls, auditEvents := s.fetchAuditCounters(r)
	writeJSON(w, http.StatusOK, map[string]any{
		"store":             st,
		"audit_calls":       auditCalls,
		"audit_event_count": auditEvents,
	})
}

// fetchAuditCounters 从独立的假审计进程读取调用计数；其不可达时返回 -1，不影响主报告。
func (s *Server) fetchAuditCounters(r *http.Request) (int, int) {
	if s.cfg.AuditBaseURL == "" {
		return -1, -1
	}
	req, err := http.NewRequestWithContext(r.Context(), http.MethodGet, s.cfg.AuditBaseURL+"/audit/inspect", nil)
	if err != nil {
		return -1, -1
	}
	resp, err := s.http.Do(req)
	if err != nil {
		return -1, -1
	}
	defer resp.Body.Close()
	var body struct {
		Calls  int        `json:"calls"`
		Events []struct{} `json:"events"`
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(&body); err != nil {
		return -1, -1
	}
	return body.Calls, len(body.Events)
}

func (s *Server) handleReset(w http.ResponseWriter, r *http.Request) {
	// POST /admin/reset：同时清空业务存储与（尽力而为）假外部系统
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "method_not_allowed", "use POST")
		return
	}
	if err := s.cfg.Store.Reset(); err != nil {
		writeError(w, http.StatusInternalServerError, "reset_failed", err.Error())
		return
	}
	s.resetAudit(r)
	writeJSON(w, http.StatusOK, map[string]string{"status": "reset"})
}

func (s *Server) resetAudit(r *http.Request) {
	if s.cfg.AuditBaseURL == "" {
		return
	}
	req, err := http.NewRequestWithContext(r.Context(), http.MethodPost, s.cfg.AuditBaseURL+"/audit/reset", nil)
	if err != nil {
		return
	}
	resp, err := s.http.Do(req)
	if err == nil {
		_ = resp.Body.Close()
	}
}

func (s *Server) handleClockAdvance(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "method_not_allowed", "use POST")
		return
	}
	if s.cfg.RealClock {
		writeError(w, http.StatusBadRequest, "real_clock", "server runs on a real clock; restart with -fake-clock to control time")
		return
	}
	fc, ok := s.cfg.Clock.(*clock.Fake)
	if !ok {
		writeError(w, http.StatusBadRequest, "not_controllable", "clock is not a fake clock")
		return
	}
	var body struct {
		MS int `json:"ms"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		writeError(w, http.StatusBadRequest, "bad_body", err.Error())
		return
	}
	t := fc.Advance(time.Duration(body.MS) * time.Millisecond)
	writeJSON(w, http.StatusOK, map[string]string{"now": t.UTC().Format(time.RFC3339Nano)})
}

// recordView 是幂等记录对外的只读视图。
func recordView(rec store.Record) map[string]any {
	v := map[string]any{
		"idempotency_key": rec.Key,
		"status":          rec.Status,
		"digest":          rec.Digest,
		"generation":      rec.Gen,
	}
	if rec.Status == store.Completed {
		v["status_code"] = rec.StatusCode
		if len(rec.Response) > 0 {
			v["response"] = json.RawMessage(rec.Response)
		}
		if rec.Effect != nil {
			v["effect"] = rec.Effect
		}
	} else {
		v["expires_at"] = rec.ExpiresAt.UTC().Format(time.RFC3339Nano)
	}
	return v
}
