// Package httpapi exposes the layered token-budget scheduler over a small
// local HTTP interface. All token amounts on the wire are exact decimal
// strings; fractional JSON numbers are rejected by the parser.
package httpapi

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"strconv"
	"strings"
	"time"

	"tokenbudget/internal/budget"
	"tokenbudget/internal/clock"
	"tokenbudget/internal/rational"
)

// Server bundles the dependencies shared by every handler.
type Server struct {
	Limiter   *budget.Limiter
	Scheduler *budget.Scheduler
	Sink      *budget.MemorySink
	Clock     clock.Clock

	mux *http.ServeMux
}

// NewServer wires routes. VirtualClock, when non-nil, enables the testing-only
// POST /internal/clock/advance endpoint.
func NewServer(l *budget.Limiter, s *budget.Scheduler, sink *budget.MemorySink, clk clock.Clock, virtual *clock.Virtual) *Server {
	srv := &Server{Limiter: l, Scheduler: s, Sink: sink, Clock: clk}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", srv.handleHealth)
	mux.HandleFunc("POST /v1/acquire", srv.handleAcquire)
	mux.HandleFunc("GET /v1/buckets/", srv.handleBucket)
	mux.HandleFunc("GET /v1/config", srv.handleGetConfig)
	mux.HandleFunc("PUT /v1/config", srv.handlePutConfig)
	mux.HandleFunc("GET /v1/events", srv.handleEvents)
	if virtual != nil {
		mux.HandleFunc("POST /internal/clock/advance", makeAdvance(virtual))
	}
	srv.mux = mux
	return srv
}

// Handler returns the root http.Handler.
func (s *Server) Handler() http.Handler { return s.mux }

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok", "now": s.Clock.Now().Format(time.RFC3339Nano)})
}

type acquireRequest struct {
	Tenant    string `json:"tenant"`
	Cost      string `json:"cost"`       // exact decimal tokens, e.g. "1", "0.25"
	TimeoutMS int64  `json:"timeout_ms"` // 0 = non-blocking
}

type acquireResponse struct {
	Allowed      bool                   `json:"allowed"`
	Reason       budget.DecisionResult  `json:"reason,omitempty"`
	RequestID    string                 `json:"request_id,omitempty"`
	Cost         string                 `json:"cost"`
	Tenant       string                 `json:"tenant"`
	At           time.Time              `json:"at"`
	RetryAfterMS int64                  `json:"retry_after_ms,omitempty"`
	Global       *budget.BucketSnapshot `json:"global,omitempty"`
	TenantSnap   *budget.BucketSnapshot `json:"tenant_bucket,omitempty"`
	TimedOut     bool                   `json:"timed_out,omitempty"`
	Error        string                 `json:"error,omitempty"`
}

func (s *Server) handleAcquire(w http.ResponseWriter, r *http.Request) {
	var req acquireRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
		return
	}
	req.Tenant = strings.TrimSpace(req.Tenant)
	if req.Tenant == "" {
		writeError(w, http.StatusBadRequest, "tenant is required")
		return
	}
	cost, err := rational.ParseDecimalTokens(req.Cost)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	if cost <= 0 {
		writeError(w, http.StatusBadRequest, "cost must be positive")
		return
	}

	if req.TimeoutMS <= 0 {
		d := s.Limiter.TryAcquire(req.Tenant, cost)
		resp := acquireResponse{
			Allowed: d.Allowed, Reason: d.Reason, RequestID: d.RequestID,
			Cost: d.Cost, Tenant: d.Tenant, At: d.At,
			RetryAfterMS: durationMS(d.RetryAfter),
			Global:       d.Global, TenantSnap: d.TenantSnap,
		}
		status := http.StatusOK
		if !d.Allowed {
			status = http.StatusTooManyRequests
		}
		writeJSON(w, status, resp)
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), time.Duration(req.TimeoutMS)*time.Millisecond)
	defer cancel()
	d, err := s.Scheduler.Limiter().Acquire(ctx, req.Tenant, cost)
	resp := acquireResponse{
		Allowed: d.Allowed, Reason: d.Reason, RequestID: d.RequestID,
		Cost: d.Cost, Tenant: d.Tenant, At: d.At,
		RetryAfterMS: durationMS(d.RetryAfter),
		Global:       d.Global, TenantSnap: d.TenantSnap,
	}
	switch {
	case err == nil:
		writeJSON(w, http.StatusOK, resp)
	case errors.Is(err, context.DeadlineExceeded):
		resp.TimedOut = true
		resp.Error = err.Error()
		writeJSON(w, http.StatusTooManyRequests, resp)
	default:
		resp.Error = err.Error()
		writeJSON(w, http.StatusTooManyRequests, resp)
	}
}

func durationMS(d time.Duration) int64 {
	if d <= 0 || d >= 1<<62 {
		return 0
	}
	return d.Milliseconds()
}

func (s *Server) handleBucket(w http.ResponseWriter, r *http.Request) {
	tenant := strings.TrimPrefix(r.URL.Path, "/v1/buckets/")
	tenant = strings.Trim(tenant, "/")
	if tenant == "" {
		writeError(w, http.StatusBadRequest, "tenant id is required")
		return
	}
	g, t, at := s.Limiter.State(tenant)
	writeJSON(w, http.StatusOK, map[string]any{
		"tenant": tenant, "at": at, "global": g, "tenant_bucket": t,
	})
}

// configRequest is the wire config. Rates may be exact strings ("10",
// "0.5", "1/3") or {"tokens":n,"per_seconds":d}; bursts are exact decimal
// strings to avoid float transport.
type configRequest struct {
	Global  configBucket            `json:"global"`
	Default configBucket            `json:"default"`
	Tenants map[string]configBucket `json:"tenants"`
}

type configBucket struct {
	Rate  rational.Rate `json:"rate"`
	Burst string        `json:"burst"`
}

func (c configBucket) toBucketConfig() (budget.BucketConfig, error) {
	burst, err := rational.ParseDecimalTokens(c.Burst)
	if err != nil {
		return budget.BucketConfig{}, err
	}
	if burst <= 0 {
		return budget.BucketConfig{}, errors.New("burst must be positive")
	}
	return budget.BucketConfig{Rate: c.Rate, BurstMicro: burst}, nil
}

func (s *Server) handleGetConfig(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, s.Limiter.Config().View())
}

func (s *Server) handlePutConfig(w http.ResponseWriter, r *http.Request) {
	var req configRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
		return
	}
	g, err := req.Global.toBucketConfig()
	if err != nil {
		writeError(w, http.StatusBadRequest, "global: "+err.Error())
		return
	}
	d, err := req.Default.toBucketConfig()
	if err != nil {
		writeError(w, http.StatusBadRequest, "default: "+err.Error())
		return
	}
	cfg := budget.Config{Global: g, Default: d}
	if len(req.Tenants) > 0 {
		cfg.Tenants = make(map[string]budget.BucketConfig, len(req.Tenants))
		for t, cb := range req.Tenants {
			bc, err := cb.toBucketConfig()
			if err != nil {
				writeError(w, http.StatusBadRequest, "tenant "+t+": "+err.Error())
				return
			}
			cfg.Tenants[t] = bc
		}
	}
	if err := s.Limiter.UpdateConfig(cfg); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, cfg.View())
}

func (s *Server) handleEvents(w http.ResponseWriter, r *http.Request) {
	tenant := r.URL.Query().Get("tenant")
	limit := 0
	if l := r.URL.Query().Get("limit"); l != "" {
		n, err := strconv.Atoi(l)
		if err != nil || n < 0 {
			writeError(w, http.StatusBadRequest, "limit must be a non-negative int")
			return
		}
		limit = n
	}
	if s.Sink == nil {
		writeJSON(w, http.StatusOK, map[string]any{"events": []budget.Event{}})
		return
	}
	evs := s.Sink.Events(tenant, limit)
	if evs == nil {
		evs = []budget.Event{}
	}
	writeJSON(w, http.StatusOK, map[string]any{"events": evs})
}

type advanceRequest struct {
	MS int64 `json:"ms"`
	NS int64 `json:"ns"`
}

func makeAdvance(v *clock.Virtual) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var req advanceRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			writeError(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
			return
		}
		d := time.Duration(req.MS)*time.Millisecond + time.Duration(req.NS)
		v.Advance(d)
		writeJSON(w, http.StatusOK, map[string]string{"now": v.Now().Format(time.RFC3339Nano)})
	}
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}
