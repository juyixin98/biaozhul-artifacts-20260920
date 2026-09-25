// Package httpserver exposes the conditional-update store over HTTP and
// provides admin endpoints for the fault-injection client: fault control
// on the fake downstream service and control of the fake clock.
package httpserver

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"regexp"
	"time"

	"conditionupdate/internal/clock"
	"conditionupdate/internal/etag"
	"conditionupdate/internal/fakesvc"
	"conditionupdate/internal/store"
)

var idPattern = regexp.MustCompile(`^[A-Za-z0-9._-]{1,128}$`)

// Server wires the store, the fake audit service and the clock into an
// http.Handler.
type Server struct {
	store *store.Store
	audit *fakesvc.AuditService
	clock clock.Clock
	fake  *clock.Fake // non-nil only when the controllable clock is enabled
	mux   *http.ServeMux
}

// New builds a Server. Pass a *clock.Fake to enable the /admin/clock
// endpoint; pass a clock.Real to disable it.
func New(s *store.Store, a *fakesvc.AuditService, c clock.Clock) *Server {
	srv := &Server{store: s, audit: a, clock: c}
	if f, ok := c.(*clock.Fake); ok {
		srv.fake = f
	}
	srv.routes()
	return srv
}

func (s *Server) routes() {
	m := http.NewServeMux()
	m.HandleFunc("GET /healthz", s.handleHealth)
	m.HandleFunc("GET /resources/{id}", s.handleGet)
	m.HandleFunc("PUT /resources/{id}", s.handlePut)
	m.HandleFunc("DELETE /resources/{id}", s.handleDelete)
	m.HandleFunc("GET /admin/state", s.handleState)
	m.HandleFunc("GET /admin/audit", s.handleAudit)
	m.HandleFunc("POST /admin/faults", s.handleSetFaults)
	m.HandleFunc("GET /admin/faults", s.handleGetFaults)
	if s.fake != nil {
		m.HandleFunc("POST /admin/clock", s.handleClock)
	}
	s.mux = m
}

// ServeHTTP implements http.Handler.
func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	s.mux.ServeHTTP(w, r)
}

type errorBody struct {
	Error struct {
		Code    string `json:"code"`
		Message string `json:"message"`
	} `json:"error"`
}

func writeError(w http.ResponseWriter, status int, code, msg string) {
	var b errorBody
	b.Error.Code = code
	b.Error.Message = msg
	writeJSON(w, status, b)
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func (s *Server) handleHealth(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func validID(w http.ResponseWriter, id string) bool {
	if !idPattern.MatchString(id) {
		writeError(w, http.StatusBadRequest, "bad_id", "resource id must match "+idPattern.String())
		return false
	}
	return true
}

func (s *Server) handleGet(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if !validID(w, id) {
		return
	}
	res, err := s.store.Get(id)
	if errors.Is(err, store.ErrNotFound) {
		writeError(w, http.StatusNotFound, "not_found", "resource does not exist")
		return
	}
	w.Header().Set("ETag", etag.ETag{Value: res.ETag}.String())
	writeJSON(w, http.StatusOK, res)
}

func (s *Server) handlePut(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if !validID(w, id) {
		return
	}

	var u store.Update
	if h := r.Header.Get("If-Match"); h != "" {
		c, err := etag.ParseCondition(h)
		if err != nil {
			writeError(w, http.StatusBadRequest, "bad_if_match", err.Error())
			return
		}
		u.IfMatch = &c
	}
	if h := r.Header.Get("If-None-Match"); h != "" {
		c, err := etag.ParseCondition(h)
		if err != nil {
			writeError(w, http.StatusBadRequest, "bad_if_none_match", err.Error())
			return
		}
		u.IfNoneMatch = &c
	}

	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, 1<<20))
	if err != nil {
		writeError(w, http.StatusRequestEntityTooLarge, "too_large", "body exceeds 1 MiB")
		return
	}
	u.Data = body

	// The audit hook runs inside the store's atomic section: if the fake
	// downstream fails, the update is aborted with zero side effects.
	hook := func(next store.Resource) error {
		return s.audit.Record(fakesvc.AuditEntry{
			ResourceID: next.ID,
			Version:    next.Version,
			ETag:       next.ETag,
			At:         s.clock.Now(),
		})
	}

	res, err := s.store.Put(id, u, hook)
	if err != nil {
		s.writeStoreError(w, err)
		return
	}
	w.Header().Set("ETag", etag.ETag{Value: res.ETag}.String())
	writeJSON(w, http.StatusOK, res)
}

func (s *Server) handleDelete(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if !validID(w, id) {
		return
	}
	var cond *etag.Condition
	if h := r.Header.Get("If-Match"); h != "" {
		c, err := etag.ParseCondition(h)
		if err != nil {
			writeError(w, http.StatusBadRequest, "bad_if_match", err.Error())
			return
		}
		cond = &c
	}
	if err := s.store.Delete(id, cond); err != nil {
		s.writeStoreError(w, err)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

func (s *Server) writeStoreError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, store.ErrNotFound):
		writeError(w, http.StatusNotFound, "not_found", err.Error())
	case errors.Is(err, store.ErrPreconditionRequired):
		writeError(w, http.StatusPreconditionRequired, "precondition_required",
			"conditional update requires If-Match or If-None-Match")
	case errors.Is(err, store.ErrPreconditionFailed):
		writeError(w, http.StatusPreconditionFailed, "precondition_failed", err.Error())
	case errors.Is(err, fakesvc.ErrInjected):
		writeError(w, http.StatusServiceUnavailable, "downstream_failure",
			"downstream audit service failed; update aborted without side effects")
	default:
		writeError(w, http.StatusInternalServerError, "internal", err.Error())
	}
}

func (s *Server) handleState(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"resources": s.store.Snapshot()})
}

func (s *Server) handleAudit(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"entries": s.audit.Entries()})
}

func (s *Server) handleSetFaults(w http.ResponseWriter, r *http.Request) {
	var f fakesvc.Faults
	if err := json.NewDecoder(r.Body).Decode(&f); err != nil {
		writeError(w, http.StatusBadRequest, "bad_body", err.Error())
		return
	}
	if f.FailNext < 0 || f.LatencyMs < 0 {
		writeError(w, http.StatusBadRequest, "bad_faults", "fault values must be >= 0")
		return
	}
	s.audit.SetFaults(f)
	writeJSON(w, http.StatusOK, f)
}

func (s *Server) handleGetFaults(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, s.audit.GetFaults())
}

type clockRequest struct {
	Set       string `json:"set"`       // RFC3339 timestamp
	AdvanceMs int64  `json:"advanceMs"` // duration to advance
}

func (s *Server) handleClock(w http.ResponseWriter, r *http.Request) {
	var req clockRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "bad_body", err.Error())
		return
	}
	if req.Set != "" {
		t, err := time.Parse(time.RFC3339Nano, req.Set)
		if err != nil {
			writeError(w, http.StatusBadRequest, "bad_time", err.Error())
			return
		}
		s.fake.Set(t)
	}
	if req.AdvanceMs != 0 {
		s.fake.Advance(time.Duration(req.AdvanceMs) * time.Millisecond)
	}
	writeJSON(w, http.StatusOK, map[string]string{"now": s.fake.Now().Format(time.RFC3339Nano)})
}
