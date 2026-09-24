// Package api exposes the task store over HTTP using only net/http.
//
// Endpoints (JSON in/out):
//
//	POST   /tasks                   submit            {payload: any}
//	GET    /tasks                   list
//	GET    /tasks/{id}              fetch one
//	POST   /tasks/claim             claim             {worker_id}
//	POST   /tasks/{id}/heartbeat    renew lease       {worker_id, lease_token}
//	POST   /tasks/{id}/complete     commit result     {worker_id, lease_token, result}
//	POST   /tasks/{id}/cancel       cancel
//	POST   /tasks/{id}/retry        give back / requeue{worker_id, lease_token?}
//
// All mutating endpoints are serialized inside the store; the moment a
// handler obtains the store lock is that request's linearization point.
package api

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"strings"
	"time"

	"taskq/internal/machine"
	"taskq/internal/store"
)

// Server bundles the HTTP handler with its dependencies.
type Server struct {
	Store  *store.Store
	Logger *log.Logger
}

// NewHandler builds the root http.Handler.
func (s *Server) NewHandler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/tasks", s.tasksRoot)
	mux.HandleFunc("/tasks/", s.taskItem)
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	return logRequests(s.Logger, mux)
}

func (s *Server) tasksRoot(w http.ResponseWriter, r *http.Request) {
	switch {
	case r.Method == http.MethodPost:
		s.submit(w, r)
	case r.Method == http.MethodGet:
		s.list(w, r)
	default:
		writeError(w, http.StatusMethodNotAllowed, "METHOD_NOT_ALLOWED", "use POST or GET on /tasks")
	}
}

func (s *Server) taskItem(w http.ResponseWriter, r *http.Request) {
	p := strings.TrimPrefix(r.URL.Path, "/tasks/")
	parts := strings.Split(p, "/")
	// /tasks/{id} or /tasks/{id}/{action}
	if parts[0] == "" || len(parts) > 2 || (len(parts) == 2 && parts[1] == "") {
		writeError(w, http.StatusNotFound, "NOT_FOUND", r.URL.Path)
		return
	}
	id := parts[0]
	if len(parts) == 1 {
		// /tasks/claim is collection-scoped; everything else is /tasks/{id}.
		if id == "claim" && r.Method == http.MethodPost {
			s.claim(w, r)
			return
		}
		switch r.Method {
		case http.MethodGet:
			s.get(w, r, id)
		default:
			writeError(w, http.StatusMethodNotAllowed, "METHOD_NOT_ALLOWED", "use GET on /tasks/{id}, or POST /tasks/claim")
		}
		return
	}
	action := parts[1]
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "METHOD_NOT_ALLOWED", "actions must be POST")
		return
	}
	switch action {
	case "heartbeat":
		s.heartbeat(w, r, id)
	case "complete":
		s.complete(w, r, id)
	case "cancel":
		s.cancel(w, r, id)
	case "retry":
		s.retry(w, r, id)
	default:
		writeError(w, http.StatusNotFound, "NOT_FOUND", "unknown action %q", action)
	}
}

// --- handlers -------------------------------------------------------------

type submitReq struct {
	Payload json.RawMessage `json:"payload"`
}

func (s *Server) submit(w http.ResponseWriter, r *http.Request) {
	var req submitReq
	if err := decodeJSON(w, r, &req); err != nil {
		writeDecodeError(w, err)
		return
	}
	t, berr := s.Store.Submit(req.Payload)
	if berr != nil {
		writeBizError(w, berr)
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{"task": viewOf(t)})
}

func (s *Server) list(w http.ResponseWriter, r *http.Request) {
	tasks, err := s.Store.List()
	if err != nil {
		writeError(w, http.StatusInternalServerError, "INTERNAL", "%v", err)
		return
	}
	views := make([]taskView, 0, len(tasks))
	for i := range tasks {
		views = append(views, viewOf(&tasks[i]))
	}
	writeJSON(w, http.StatusOK, map[string]any{"tasks": views})
}

func (s *Server) get(w http.ResponseWriter, r *http.Request, id string) {
	t := s.Store.Get(id)
	if t == nil {
		writeError(w, http.StatusNotFound, "NOT_FOUND", "task %q not found", id)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"task": viewOf(t)})
}

type claimReq struct {
	WorkerID string `json:"worker_id"`
}

func (s *Server) claim(w http.ResponseWriter, r *http.Request) {
	var req claimReq
	if err := decodeJSON(w, r, &req); err != nil {
		writeDecodeError(w, err)
		return
	}
	if strings.TrimSpace(req.WorkerID) == "" {
		writeError(w, http.StatusBadRequest, "BAD_REQUEST", "worker_id is required")
		return
	}
	t, token, berr := s.Store.Claim(req.WorkerID)
	if berr != nil {
		writeBizError(w, berr)
		return
	}
	v := viewOf(t)
	writeJSON(w, http.StatusOK, map[string]any{"task": v, "lease_token": token})
}

type leaseReq struct {
	WorkerID   string          `json:"worker_id"`
	LeaseToken string          `json:"lease_token"`
	Result     json.RawMessage `json:"result,omitempty"`
}

func (s *Server) heartbeat(w http.ResponseWriter, r *http.Request, id string) {
	var req leaseReq
	if err := decodeJSON(w, r, &req); err != nil {
		writeDecodeError(w, err)
		return
	}
	t, berr := s.Store.Heartbeat(id, req.WorkerID, req.LeaseToken)
	if berr != nil {
		writeBizError(w, berr)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"task": viewOf(t)})
}

func (s *Server) complete(w http.ResponseWriter, r *http.Request, id string) {
	var req leaseReq
	if err := decodeJSON(w, r, &req); err != nil {
		writeDecodeError(w, err)
		return
	}
	t, berr := s.Store.Complete(id, req.WorkerID, req.LeaseToken, req.Result)
	if berr != nil {
		writeBizError(w, berr)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"task": viewOf(t)})
}

func (s *Server) cancel(w http.ResponseWriter, r *http.Request, id string) {
	// A request body is optional and ignored; still drain it through the
	// size-limited reader so an oversized body is rejected.
	_ = decodeJSON(w, r, &struct{}{})
	t, berr := s.Store.Cancel(id)
	if berr != nil {
		writeBizError(w, berr)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"task": viewOf(t)})
}

func (s *Server) retry(w http.ResponseWriter, r *http.Request, id string) {
	// An empty body is allowed: retry of a PENDING task needs no lease.
	var req leaseReq
	if err := decodeJSON(w, r, &req); err != nil {
		writeDecodeError(w, err)
		return
	}
	t, berr := s.Store.Retry(id, req.WorkerID, req.LeaseToken)
	if berr != nil {
		writeBizError(w, berr)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"task": viewOf(t)})
}

// taskView is the public JSON projection. The lease token is only returned
// in the claim response (a separate top-level field), never as task state.
type taskView struct {
	ID            string          `json:"id"`
	State         machine.State   `json:"state"`
	Attempt       int             `json:"attempt"`
	WorkerID      string          `json:"worker_id,omitempty"`
	LeaseDeadline *time.Time      `json:"lease_deadline,omitempty"`
	Payload       json.RawMessage `json:"payload,omitempty"`
	Result        json.RawMessage `json:"result,omitempty"`
	Version       int64           `json:"version"`
	CreatedAt     time.Time       `json:"created_at"`
	UpdatedAt     time.Time       `json:"updated_at"`
}

func viewOf(t *machine.Task) taskView {
	v := taskView{
		ID:        t.ID,
		State:     t.State,
		Attempt:   t.Attempt,
		WorkerID:  t.WorkerID,
		Payload:   t.Payload,
		Result:    t.Result,
		Version:   t.Version,
		CreatedAt: t.CreatedAt,
		UpdatedAt: t.UpdatedAt,
	}
	if !t.LeaseDeadline.IsZero() {
		d := t.LeaseDeadline
		v.LeaseDeadline = &d
	}
	return v
}

// --- plumbing -------------------------------------------------------------

func decodeBody(w http.ResponseWriter, r *http.Request) ([]byte, error) {
	// Limit the request body to 1 MiB; wrapping with the live ResponseWriter
	// lets the server emit a 413-style read error instead of buffering forever.
	r.Body = http.MaxBytesReader(w, r.Body, 1<<20)
	b, err := io.ReadAll(r.Body)
	if err != nil {
		return nil, err
	}
	if len(b) == 0 {
		return nil, nil
	}
	return b, nil
}

// decodeJSON reads, limits and unmarshals the request body with unknown fields
// rejected. An empty body is allowed and leaves dst untouched.
func decodeJSON(w http.ResponseWriter, r *http.Request, dst any) error {
	b, err := decodeBody(w, r)
	if err != nil {
		return err
	}
	if len(b) == 0 {
		return nil
	}
	dec := json.NewDecoder(bytes.NewReader(b))
	dec.DisallowUnknownFields()
	if err := dec.Decode(dst); err != nil {
		return err
	}
	if dec.More() {
		return fmt.Errorf("unexpected trailing JSON value")
	}
	return nil
}

type errBody struct {
	Error struct {
		Code    machine.ErrorCode `json:"code"`
		Message string            `json:"message"`
	} `json:"error"`
}

// writeDecodeError maps a body-decoding failure to the right status: 413 for
// an oversized body, 400 for malformed JSON.
func writeDecodeError(w http.ResponseWriter, err error) {
	var maxErr *http.MaxBytesError
	if errors.As(err, &maxErr) {
		writeError(w, http.StatusRequestEntityTooLarge, "PAYLOAD_TOO_LARGE",
			"request body exceeds 1 MiB")
		return
	}
	writeError(w, http.StatusBadRequest, "BAD_REQUEST", "invalid JSON body: %v", err)
}

func writeBizError(w http.ResponseWriter, e *machine.BizError) {
	writeError(w, e.HTTPStatus, e.Code, "%s", e.Message)
}

func writeError(w http.ResponseWriter, status int, code machine.ErrorCode, format string, args ...any) {
	var b errBody
	b.Error.Code = code
	b.Error.Message = fmt.Sprintf(format, args...)
	writeJSON(w, status, b)
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	_ = enc.Encode(v)
}

func logRequests(lg *log.Logger, h http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		rw := &statusWriter{ResponseWriter: w, status: 200}
		h.ServeHTTP(rw, r)
		if lg != nil {
			lg.Printf("%s %s -> %d (%s)", r.Method, r.URL.RequestURI(), rw.status, time.Since(start))
		}
	})
}

type statusWriter struct {
	http.ResponseWriter
	status int
}

func (s *statusWriter) WriteHeader(code int) {
	s.status = code
	s.ResponseWriter.WriteHeader(code)
}
