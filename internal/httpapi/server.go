// Package httpapi exposes the budget scheduler over a small local HTTP API.
//
// Endpoints:
//
//	GET  /healthz
//	GET  /state
//	POST /request                 immediate two-layer take
//	POST /schedule                reserve + delayed execution
//	GET  /jobs                    list jobs
//	GET  /jobs/{id}               one job
//	PUT  /config/global           replace global config
//	PUT  /config/tenants/{id}     create/replace one tenant
//	GET  /events                  recorded state-change events
package httpapi

import (
	"encoding/json"
	"errors"
	"net/http"
	"strings"
	"sync"

	"tokenbudget/internal/budget"
	"tokenbudget/internal/event"
)

// Server bundles the limiter, scheduler and event memory behind one handler.
type Server struct {
	Limiter   *budget.Limiter
	Scheduler *budget.Scheduler
	Mem       *event.MemorySink

	mu      sync.Mutex
	execCnt int64
}

type rateDTO struct {
	RateNum int64 `json:"rate_num"`
	RateDen int64 `json:"rate_den_ns"`
}

type configDTO struct {
	Rate          rateDTO `json:"rate"`
	Capacity      int64   `json:"capacity"`
	InitialTokens *int64  `json:"initial_tokens,omitempty"`
}

func (c configDTO) toConfig() budget.Config {
	return budget.Config{
		Rate:          budget.Rate{Num: c.Rate.RateNum, Den: c.Rate.RateDen},
		Capacity:      c.Capacity,
		InitialTokens: c.InitialTokens,
	}
}

type requestDTO struct {
	Tenant string `json:"tenant"`
	Tokens int64  `json:"tokens"`
}

type scheduleDTO struct {
	Tenant string `json:"tenant"`
	Name   string `json:"name"`
	Tokens int64  `json:"tokens"`
}

type takeResp struct {
	Allowed bool             `json:"allowed"`
	Reason  string           `json:"reason,omitempty"`
	WaitNS  int64            `json:"wait_ns"`
	State   budget.StateView `json:"state"`
}

type scheduleResp struct {
	Accepted bool       `json:"accepted"`
	Job      budget.Job `json:"job"`
	WaitNS   int64      `json:"wait_ns"`
	Reason   string     `json:"reason,omitempty"`
}

// NewMux wires all routes.
func (s *Server) NewMux() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", s.health)
	mux.HandleFunc("GET /state", s.state)
	mux.HandleFunc("POST /request", s.request)
	mux.HandleFunc("POST /schedule", s.schedule)
	mux.HandleFunc("GET /jobs", s.jobs)
	mux.HandleFunc("GET /jobs/{id}", s.job)
	mux.HandleFunc("PUT /config/global", s.configGlobal)
	mux.HandleFunc("PUT /config/tenants/{id}", s.configTenant)
	mux.HandleFunc("GET /events", s.events)
	return mux
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, code int, msg string) {
	writeJSON(w, code, map[string]string{"error": msg})
}

func (s *Server) health(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *Server) state(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, s.Limiter.Snapshot())
}

func (s *Server) request(w http.ResponseWriter, r *http.Request) {
	var in requestDTO
	if err := json.NewDecoder(r.Body).Decode(&in); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
		return
	}
	if in.Tenant == "" {
		writeErr(w, http.StatusBadRequest, "tenant is required")
		return
	}
	d := s.Limiter.TryTake(in.Tenant, in.Tokens)
	resp := takeResp{Allowed: d.Allowed, Reason: d.Reason, WaitNS: d.WaitNS, State: s.Limiter.Snapshot()}
	if d.Allowed {
		writeJSON(w, http.StatusOK, resp)
		return
	}
	if d.Reason == budget.ReasonExceedsCap {
		writeJSON(w, http.StatusUnprocessableEntity, resp)
		return
	}
	w.Header().Set("Retry-After-Ns", itoa(d.WaitNS))
	writeJSON(w, http.StatusTooManyRequests, resp)
}

func (s *Server) schedule(w http.ResponseWriter, r *http.Request) {
	var in scheduleDTO
	if err := json.NewDecoder(r.Body).Decode(&in); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
		return
	}
	if in.Tenant == "" {
		writeErr(w, http.StatusBadRequest, "tenant is required")
		return
	}
	name := in.Name
	if name == "" {
		name = "noop"
	}
	h, err := s.Scheduler.Submit(in.Tenant, name, in.Tokens, func() error {
		// The backend executes no real work; this counts as the executed unit.
		s.mu.Lock()
		s.execCnt++
		s.mu.Unlock()
		return nil
	})
	if err != nil {
		j := h.Result()
		resp := scheduleResp{Accepted: false, Job: j, Reason: denyReason(err)}
		if errors.Is(err, budget.ErrTokensExceedCap) || errors.Is(err, budget.ErrZeroTokens) {
			writeJSON(w, http.StatusUnprocessableEntity, resp)
			return
		}
		writeJSON(w, http.StatusTooManyRequests, resp)
		return
	}
	j := h.Result()
	writeJSON(w, http.StatusAccepted, scheduleResp{
		Accepted: true, Job: j, WaitNS: j.WaitNS,
	})
}

func denyReason(err error) string {
	switch {
	case errors.Is(err, budget.ErrTokensExceedCap):
		return budget.ReasonExceedsCap
	case errors.Is(err, budget.ErrZeroTokens):
		return budget.ReasonExceedsCap
	default:
		return budget.ReasonInsufficient
	}
}

func (s *Server) jobs(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"jobs": s.Scheduler.Jobs()})
}

func (s *Server) job(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	j, ok := s.Scheduler.Job(id)
	if !ok {
		writeErr(w, http.StatusNotFound, "unknown job")
		return
	}
	writeJSON(w, http.StatusOK, j)
}

func (s *Server) configGlobal(w http.ResponseWriter, r *http.Request) {
	var in configDTO
	if err := json.NewDecoder(r.Body).Decode(&in); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
		return
	}
	if err := s.Limiter.UpdateGlobalConfig(in.toConfig()); err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, s.Limiter.Snapshot())
}

func (s *Server) configTenant(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if strings.TrimSpace(id) == "" {
		writeErr(w, http.StatusBadRequest, "tenant id required")
		return
	}
	var in configDTO
	if err := json.NewDecoder(r.Body).Decode(&in); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
		return
	}
	if err := s.Limiter.UpdateTenantConfig(id, in.toConfig()); err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, s.Limiter.Snapshot())
}

func (s *Server) events(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"events": s.Mem.Events()})
}

func itoa(v int64) string {
	if v == 0 {
		return "0"
	}
	neg := v < 0
	if neg {
		v = -v
	}
	var b [24]byte
	i := len(b)
	for v > 0 {
		i--
		b[i] = byte('0' + v%10)
		v /= 10
	}
	if neg {
		i--
		b[i] = '-'
	}
	return string(b[i:])
}
