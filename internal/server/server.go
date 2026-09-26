// Package server exposes the resource store over HTTP using conditional
// requests:
//
//	POST   /resources/{key}   create the initial representation
//	GET    /resources/{key}   read it (200 + ETag, or 404)
//	PUT    /resources/{key}   update; REQUIRES If-Match (strong)
//	DELETE /resources/{key}   delete; REQUIRES If-Match (strong)
//	GET    /audit             inspect the in-process fake audit log
//
// Status mapping:
//
//	200/201/204 success, ETag response header carries the strong validator
//	404         no current representation (GET)
//	409         POST to an existing live key
//	412         If-Match was supplied but failed (mismatch, weak tag, or "*"
//	            on a missing resource)
//	428         PUT/DELETE without any If-Match precondition
//	503         the (fake) downstream side effect failed; nothing was mutated
package server

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"

	"etagrace/internal/clock"
	"etagrace/internal/etag"
	"etagrace/internal/notifier"
	"etagrace/internal/store"
)

// maxBodyBytes caps request bodies (1 MiB); this service is a local demo.
const maxBodyBytes = 1 << 20

// Server wires the store, clock and audit notifier to an http.Handler.
type Server struct {
	store   *store.Store
	clk     clock.Clock
	notif   notifier.Notifier
	audit   *notifier.AuditLog
	handler http.Handler
}

// New constructs the server. notif is invoked atomically as part of every
// successful state change; pass a retrying wrapper to tolerate transient
// injected faults.
func New(st *store.Store, clk clock.Clock, notif notifier.Notifier, audit *notifier.AuditLog) *Server {
	s := &Server{store: st, clk: clk, notif: notif, audit: audit}

	mux := http.NewServeMux()
	mux.HandleFunc("/resources/", s.handleResource)
	mux.HandleFunc("/audit", s.handleAudit)
	s.handler = mux
	return s
}

func (s *Server) Handler() http.Handler { return s.handler }

func (s *Server) handleAudit(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "method_not_allowed", "use GET")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"events": s.audit.Events()})
}

func (s *Server) handleResource(w http.ResponseWriter, r *http.Request) {
	key := strings.TrimPrefix(r.URL.Path, "/resources/")
	if key == "" || strings.Contains(key, "/") {
		writeError(w, http.StatusNotFound, "not_found", "resource key missing")
		return
	}

	switch r.Method {
	case http.MethodPost:
		s.create(w, r, key)
	case http.MethodGet:
		s.get(w, r, key)
	case http.MethodPut:
		s.put(w, r, key)
	case http.MethodDelete:
		s.delete(w, r, key)
	default:
		writeError(w, http.StatusMethodNotAllowed, "method_not_allowed", "allowed: GET POST PUT DELETE")
	}
}

func (s *Server) create(w http.ResponseWriter, r *http.Request, key string) {
	body, ok := readBody(w, r)
	if !ok {
		return
	}
	snap, err := s.store.Create(key, body)
	if errors.Is(err, store.ErrAlreadyExists) {
		writeError(w, http.StatusConflict, "already_exists",
			"resource exists; GET it and PUT with its ETag")
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	s.fire(r.Context(), notifier.Event{Type: "create", Key: key, Version: snap.Version, ETag: snap.Tag.String()})
	w.Header().Set("ETag", snap.Tag.String())
	w.WriteHeader(http.StatusCreated)
}

func (s *Server) get(w http.ResponseWriter, _ *http.Request, key string) {
	snap, ok := s.store.Get(key)
	if !ok {
		writeError(w, http.StatusNotFound, "not_found", "no current representation")
		return
	}
	w.Header().Set("ETag", snap.Tag.String())
	w.Header().Set("Last-Modified", snap.UpdatedAt.UTC().Format(http.TimeFormat))
	w.Header().Set("Content-Type", "application/octet-stream")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(snap.Content)
}

func (s *Server) put(w http.ResponseWriter, r *http.Request, key string) {
	tags, anyTag, ok := requirePrecondition(w, r)
	if !ok {
		return
	}
	body, ok := readBody(w, r)
	if !ok {
		return
	}

	hook := func(version int64) error {
		return s.fire(r.Context(), notifier.Event{
			Type: "update", Key: key, Version: version,
			ETag: etag.New(version, body).String(),
		})
	}
	res := s.store.ConditionalPut(key, tags, anyTag, body, hook)
	s.renderMatch(w, res, http.StatusOK)
}

func (s *Server) delete(w http.ResponseWriter, r *http.Request, key string) {
	tags, anyTag, ok := requirePrecondition(w, r)
	if !ok {
		return
	}
	hook := func(version int64) error {
		return s.fire(r.Context(), notifier.Event{Type: "delete", Key: key, Version: version, ETag: ""})
	}
	res := s.store.ConditionalDelete(key, tags, anyTag, hook)
	if res.Committed {
		w.WriteHeader(http.StatusNoContent)
		return
	}
	s.renderMatch(w, res, http.StatusOK)
}

// renderMatch translates a store.MatchResult into HTTP semantics. Success
// status for PUT is passed in; delete handles its own 204.
func (s *Server) renderMatch(w http.ResponseWriter, res store.MatchResult, okStatus int) {
	switch res.Reason {
	case "":
		w.Header().Set("ETag", res.Snapshot.Tag.String())
		w.WriteHeader(okStatus)
	case "not_existing":
		writeError(w, http.StatusPreconditionFailed, "precondition_failed",
			"If-Match named a resource with no current representation")
	case "precondition_failed":
		w.Header().Set("ETag", res.Snapshot.Tag.String()) // current validator, for retry
		writeError(w, http.StatusPreconditionFailed, "precondition_failed",
			"resource version changed or a weak validator was used for a strong comparison")
	case "hook_failed":
		writeError(w, http.StatusServiceUnavailable, "downstream_unavailable",
			"side effect failed; resource was not modified: "+res.HookErr.Error())
	default:
		writeError(w, http.StatusInternalServerError, "internal", "unknown result: "+res.Reason)
	}
}

// fire invokes the audit notifier, stamping the event time on the shared clock.
func (s *Server) fire(ctx context.Context, e notifier.Event) error {
	e.OccurredAt = s.clk.Now()
	return s.notif.Notify(ctx, e)
}

// requirePrecondition enforces RFC 9110: state changes without If-Match get
// 428. Malformed tags or weak tags are rejected: a weak validator MUST NOT be
// used for the strong comparison a state change requires.
func requirePrecondition(w http.ResponseWriter, r *http.Request) ([]etag.ETag, bool, bool) {
	raw := r.Header.Get("If-Match")
	if strings.TrimSpace(raw) == "" {
		writeError(w, http.StatusPreconditionRequired, "precondition_required",
			"send If-Match: \"<etag>\" (or \"*\") with PUT/DELETE")
		return nil, false, false
	}
	tags, anyTag, err := etag.ParseList(raw)
	if err != nil {
		writeError(w, http.StatusBadRequest, "bad_precondition", "malformed If-Match: "+err.Error())
		return nil, false, false
	}
	for _, t := range tags {
		if t.Weak {
			writeError(w, http.StatusPreconditionFailed, "weak_etag_rejected",
				"W/ weak validators cannot be used for strong comparison on PUT/DELETE")
			return nil, false, false
		}
	}
	return tags, anyTag, true
}

func readBody(w http.ResponseWriter, r *http.Request) ([]byte, bool) {
	defer r.Body.Close()
	body, err := decodeLimit(w, r)
	if err != nil {
		writeError(w, http.StatusBadRequest, "bad_body", err.Error())
		return nil, false
	}
	return body, true
}

// RetryingNotifier calls the inner notifier with capped exponential backoff.
// It is the server's defense against transient downstream faults: the store
// invokes it inside the commit critical section, so only a notification that
// ultimately succeeds allows the version/content commit.
type RetryingNotifier struct {
	Inner       notifier.Notifier
	Clk         clock.Clock
	Attempts    int
	BaseBackoff time.Duration
	MaxBackoff  time.Duration
}

func (r RetryingNotifier) Notify(ctx context.Context, e notifier.Event) error {
	attempts := r.Attempts
	if attempts < 1 {
		attempts = 1
	}
	var err error
	for attempt := 1; attempt <= attempts; attempt++ {
		err = r.Inner.Notify(ctx, e)
		if err == nil {
			return nil
		}
		if attempt == attempts {
			break
		}
		select {
		case <-ctx.Done():
			return fmt.Errorf("audit notify cancelled: %w", ctx.Err())
		default:
		}
		r.Clk.Sleep(r.backoff(attempt))
	}
	return fmt.Errorf("audit notify failed after %d attempts: %w", attempts, err)
}

func (r RetryingNotifier) backoff(attempt int) time.Duration {
	base := r.BaseBackoff
	if base <= 0 {
		base = time.Millisecond
	}
	d := base << (attempt - 1)
	if r.MaxBackoff > 0 && d > r.MaxBackoff {
		d = r.MaxBackoff
	}
	return d
}

type errorBody struct {
	Error   string `json:"error"`
	Message string `json:"message"`
}

func writeError(w http.ResponseWriter, status int, code, msg string) {
	writeJSON(w, status, errorBody{Error: code, Message: msg})
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}
