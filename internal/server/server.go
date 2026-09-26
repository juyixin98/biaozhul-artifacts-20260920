// Package server exposes the multi-tenant compute service over HTTP.
// Tenant identity comes only from the test auth header; any tenant field in
// a request body is rejected as an override attempt.
package server

import (
	"encoding/json"
	"errors"
	"net/http"
	"regexp"
	"strconv"
	"time"

	"tenantiso/internal/auth"
	"tenantiso/internal/cache"
	"tenantiso/internal/clock"
	"tenantiso/internal/engine"
)

const (
	defaultWaitTimeout = 5 * time.Second
	maxWaitTimeout     = 30 * time.Second
	maxKeyLen          = 128
)

var keyPattern = regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9._/-]{0,127}$`)

// Server wires the engine and cache to HTTP handlers.
type Server struct {
	eng    *engine.Scheduler
	cache  *cache.Cache
	clk    clock.Clock
	mux    *http.ServeMux
	limits engine.Limits
}

// New builds a Server with all routes registered.
func New(eng *engine.Scheduler, c *cache.Cache, clk clock.Clock, limits engine.Limits) *Server {
	s := &Server{eng: eng, cache: c, clk: clk, limits: limits}
	mux := http.NewServeMux()
	mux.Handle("POST /v1/compute", auth.Middleware(http.HandlerFunc(s.handleCompute)))
	mux.Handle("GET /v1/jobs/{id}", auth.Middleware(http.HandlerFunc(s.handleGetJob)))
	mux.Handle("DELETE /v1/jobs/{id}", auth.Middleware(http.HandlerFunc(s.handleCancelJob)))
	mux.Handle("GET /v1/cache/{key}", auth.Middleware(http.HandlerFunc(s.handleGetCache)))
	mux.Handle("GET /v1/stats", auth.Middleware(http.HandlerFunc(s.handleStats)))
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	s.mux = mux
	return s
}

func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	s.mux.ServeHTTP(w, r)
}

type errorBody struct {
	Error struct {
		Code    string `json:"code"`
		Message string `json:"message"`
	} `json:"error"`
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, status int, code, msg string) {
	var b errorBody
	b.Error.Code = code
	b.Error.Message = msg
	writeJSON(w, status, b)
}

// computeRequest is the client-supplied body. Tenant fields are declared so
// their presence can be *rejected*: identity must come from the auth header.
type computeRequest struct {
	Key           string  `json:"key"`
	Work          int     `json:"work"`
	MemBytes      int64   `json:"mem_bytes"`
	TenantID      *string `json:"tenant_id"`
	TenantIDCamel *string `json:"tenantId"`
}

func (s *Server) handleCompute(w http.ResponseWriter, r *http.Request) {
	tenant, _ := auth.TenantFrom(r.Context())

	var req computeRequest
	dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, "INVALID_BODY", "request body is not valid JSON for the compute schema")
		return
	}
	if req.TenantID != nil || req.TenantIDCamel != nil {
		writeErr(w, http.StatusBadRequest, "TENANT_OVERRIDE_REJECTED",
			"tenant identity is taken from the "+auth.HeaderTenant+" header only; body tenant fields are forbidden")
		return
	}
	if req.Key == "" || len(req.Key) > maxKeyLen || !keyPattern.MatchString(req.Key) {
		writeErr(w, http.StatusBadRequest, "INVALID_KEY", "key is required and must match "+keyPattern.String())
		return
	}
	if req.Work < 1 || req.Work > s.limits.MaxWork {
		writeErr(w, http.StatusUnprocessableEntity, "INVALID_WORK",
			"work must be between 1 and "+strconv.Itoa(s.limits.MaxWork))
		return
	}
	if req.MemBytes < 0 || req.MemBytes > s.limits.MaxJobMemBytes {
		writeErr(w, http.StatusUnprocessableEntity, "INVALID_MEM_BYTES",
			"mem_bytes must be between 0 and "+strconv.FormatInt(s.limits.MaxJobMemBytes, 10))
		return
	}

	job, err := s.eng.Submit(tenant, req.Key, req.Work, req.MemBytes)
	if err != nil {
		switch {
		case errors.Is(err, engine.ErrQueueFull):
			writeErr(w, http.StatusTooManyRequests, "QUEUE_FULL",
				"tenant queue is full; other tenants are unaffected")
		case errors.Is(err, engine.ErrJobTooLarge):
			writeErr(w, http.StatusUnprocessableEntity, "JOB_TOO_LARGE", err.Error())
		default:
			writeErr(w, http.StatusServiceUnavailable, "ENGINE_UNAVAILABLE", err.Error())
		}
		return
	}

	if r.URL.Query().Get("wait") == "true" {
		s.awaitJob(w, r, job)
		return
	}
	writeJSON(w, http.StatusAccepted, job.Snapshot())
}

// awaitJob blocks until the job is terminal, the client disconnects (which
// cancels the job), or the wait timeout elapses.
func (s *Server) awaitJob(w http.ResponseWriter, r *http.Request, job *engine.Job) {
	timeout := defaultWaitTimeout
	if ms := r.URL.Query().Get("timeout_ms"); ms != "" {
		if v, err := strconv.Atoi(ms); err == nil && v > 0 {
			timeout = time.Duration(v) * time.Millisecond
		}
	}
	if timeout > maxWaitTimeout {
		timeout = maxWaitTimeout
	}
	select {
	case <-job.Done():
		writeJSON(w, http.StatusOK, job.Snapshot())
	case <-r.Context().Done():
		// Client went away: cancel the job so it does not consume budget.
		s.eng.Cancel(job.ID, job.TenantID)
	case <-s.clk.After(timeout):
		writeJSON(w, http.StatusAccepted, job.Snapshot())
	}
}

func (s *Server) handleGetJob(w http.ResponseWriter, r *http.Request) {
	tenant, _ := auth.TenantFrom(r.Context())
	job, ok := s.eng.Get(r.PathValue("id"), tenant)
	if !ok {
		writeErr(w, http.StatusNotFound, "JOB_NOT_FOUND", "no such job for this tenant")
		return
	}
	writeJSON(w, http.StatusOK, job.Snapshot())
}

func (s *Server) handleCancelJob(w http.ResponseWriter, r *http.Request) {
	tenant, _ := auth.TenantFrom(r.Context())
	job, ok := s.eng.Cancel(r.PathValue("id"), tenant)
	if !ok {
		writeErr(w, http.StatusNotFound, "JOB_NOT_FOUND", "no such job for this tenant")
		return
	}
	writeJSON(w, http.StatusOK, job.Snapshot())
}

func (s *Server) handleGetCache(w http.ResponseWriter, r *http.Request) {
	tenant, _ := auth.TenantFrom(r.Context())
	key := r.PathValue("key")
	val, ok, err := s.cache.Get(r.Context(), tenant, key)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "CACHE_ERROR", err.Error())
		return
	}
	if !ok {
		writeErr(w, http.StatusNotFound, "KEY_NOT_FOUND", "no cached value for this tenant and key")
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{
		"cache_key": s.cache.ScopedKey(tenant, key),
		"value":     string(val),
	})
}

func (s *Server) handleStats(w http.ResponseWriter, r *http.Request) {
	tenant, _ := auth.TenantFrom(r.Context())
	all := s.eng.Stats()
	st, ok := all[tenant]
	if !ok {
		st = engine.TenantStats{}
	}
	// A tenant sees only its own row; the isolation boundary applies to
	// observability too.
	writeJSON(w, http.StatusOK, map[string]any{
		"tenant": tenant,
		"stats":  st,
		"limits": s.limits,
	})
}
