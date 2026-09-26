// Package server wires the HTTP API: compute submission, job status and
// cancellation, tenant-scoped cache reads, and the fault-injection
// admin endpoint used by the fault client.
package server

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"

	"example.com/tenantiso/internal/auth"
	"example.com/tenantiso/internal/backend"
	"example.com/tenantiso/internal/cache"
	"example.com/tenantiso/internal/jobs"
)

// Server holds the HTTP dependencies.
type Server struct {
	mgr     *jobs.Manager
	cache   *cache.Cache
	backend *backend.Fake
	mux     *http.ServeMux
}

// New builds the route table. All /v1 routes require tenant auth.
func New(mgr *jobs.Manager, c *cache.Cache, be *backend.Fake) *Server {
	s := &Server{mgr: mgr, cache: c, backend: be, mux: http.NewServeMux()}
	s.mux.Handle("POST /v1/compute", auth.Middleware(http.HandlerFunc(s.handleCompute)))
	s.mux.Handle("GET /v1/jobs/{id}", auth.Middleware(http.HandlerFunc(s.handleGetJob)))
	s.mux.Handle("DELETE /v1/jobs/{id}", auth.Middleware(http.HandlerFunc(s.handleCancelJob)))
	s.mux.Handle("GET /v1/cache/{key}", auth.Middleware(http.HandlerFunc(s.handleGetCache)))
	s.mux.HandleFunc("GET /admin/faults", s.handleGetFaults)
	s.mux.HandleFunc("PUT /admin/faults", s.handlePutFaults)
	return s
}

// ServeHTTP implements http.Handler.
func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) { s.mux.ServeHTTP(w, r) }

// computeRequest is the client-supplied body. There is deliberately no
// tenant field; one present in the raw body is rejected.
type computeRequest struct {
	Key        string `json:"key"`
	Payload    string `json:"payload"`
	Iterations int    `json:"iterations"`
	MemBytes   int64  `json:"mem_bytes"`
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, code int, msg string) {
	writeJSON(w, code, map[string]string{"error": msg})
}

// handleCompute admits a CPU compute job for the authenticated tenant.
func (s *Server) handleCompute(w http.ResponseWriter, r *http.Request) {
	body, err := io.ReadAll(io.LimitReader(r.Body, 1<<20))
	if err != nil {
		writeErr(w, http.StatusBadRequest, "unreadable body")
		return
	}
	// Reject any client attempt to override the tenant identity: the
	// tenant comes only from the auth context.
	var raw map[string]json.RawMessage
	if err := json.Unmarshal(body, &raw); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid JSON body")
		return
	}
	for _, forbidden := range []string{"tenant_id", "tenantId", "tenant"} {
		if _, ok := raw[forbidden]; ok {
			writeErr(w, http.StatusBadRequest, "tenant identity is set by the auth context and cannot be overridden by the request body")
			return
		}
	}
	var req computeRequest
	if err := json.Unmarshal(body, &req); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid JSON body")
		return
	}
	if req.Key == "" || req.Iterations <= 0 || req.MemBytes <= 0 {
		writeErr(w, http.StatusBadRequest, "key, iterations>0 and mem_bytes>0 are required")
		return
	}
	job, err := s.mgr.Submit(r.Context(), req.Key, req.Payload, req.Iterations, req.MemBytes)
	switch {
	case errors.Is(err, jobs.ErrQueueFull):
		writeErr(w, http.StatusTooManyRequests, err.Error())
	case errors.Is(err, jobs.ErrMemBudget):
		writeErr(w, http.StatusTooManyRequests, err.Error())
	case err != nil:
		writeErr(w, http.StatusInternalServerError, err.Error())
	default:
		writeJSON(w, http.StatusAccepted, job)
	}
}

// handleGetJob returns the job if it belongs to the caller's tenant.
func (s *Server) handleGetJob(w http.ResponseWriter, r *http.Request) {
	s.withJob(w, r, func(job *jobs.Job) { writeJSON(w, http.StatusOK, job) })
}

// handleCancelJob cancels the job if it belongs to the caller's tenant.
func (s *Server) handleCancelJob(w http.ResponseWriter, r *http.Request) {
	tenantID, _ := auth.TenantFrom(r.Context())
	job, err := s.mgr.Cancel(r.Context(), r.PathValue("id"))
	s.respondJobOp(w, job, err, tenantID)
}

func (s *Server) withJob(w http.ResponseWriter, r *http.Request, ok func(*jobs.Job)) {
	tenantID, _ := auth.TenantFrom(r.Context())
	job, err := s.mgr.Get(r.Context(), r.PathValue("id"))
	s.respondJobOp(w, job, err, tenantID, ok)
}

// respondJobOp maps job-manager errors to status codes. Cross-tenant
// access is reported as 404 to avoid leaking other tenants' job IDs.
func (s *Server) respondJobOp(w http.ResponseWriter, job *jobs.Job, err error, _ string, ok ...func(*jobs.Job)) {
	switch {
	case errors.Is(err, jobs.ErrNotFound), errors.Is(err, jobs.ErrNotOwned):
		writeErr(w, http.StatusNotFound, "job not found")
	case errors.Is(err, jobs.ErrTerminated):
		writeErr(w, http.StatusConflict, err.Error())
	case err != nil:
		writeErr(w, http.StatusInternalServerError, err.Error())
	default:
		if len(ok) > 0 {
			ok[0](job)
			return
		}
		writeJSON(w, http.StatusOK, job)
	}
}

// handleGetCache reads the caller's tenant-scoped cache entry.
func (s *Server) handleGetCache(w http.ResponseWriter, r *http.Request) {
	tenantID, _ := auth.TenantFrom(r.Context())
	key := r.PathValue("key")
	if strings.Contains(key, "|") {
		writeErr(w, http.StatusBadRequest, "key must not contain the isolation separator")
		return
	}
	v, ok := s.cache.Get(tenantID, key)
	if !ok {
		writeErr(w, http.StatusNotFound, "cache miss")
		return
	}
	w.Header().Set("Content-Type", "application/octet-stream")
	_, _ = io.Copy(w, bytes.NewReader(v))
}

// handleGetFaults reports the active fault-injection settings.
func (s *Server) handleGetFaults(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, s.backend.GetFaults())
}

// handlePutFaults replaces the fault-injection settings (fault client).
func (s *Server) handlePutFaults(w http.ResponseWriter, r *http.Request) {
	var f backend.Faults
	if err := json.NewDecoder(r.Body).Decode(&f); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid JSON body")
		return
	}
	if f.Latency < 0 || f.FailNext < 0 {
		writeErr(w, http.StatusBadRequest, "latency and fail_next must be >= 0")
		return
	}
	s.backend.SetFaults(f)
	writeJSON(w, http.StatusOK, f)
}
