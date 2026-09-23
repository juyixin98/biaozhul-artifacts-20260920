// Package api exposes the store over HTTP (JSON in, JSON out).
package api

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"time"

	"github.com/example/snapprune/internal/store"
)

// Server wires HTTP handlers to a store.Store.
type Server struct {
	st      *store.Store
	handler http.Handler
}

// NewServer builds the route table.
func NewServer(st *store.Store) *Server {
	s := &Server{st: st}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", s.healthz)
	mux.HandleFunc("POST /v1/blocks", s.postBlock)
	mux.HandleFunc("GET /v1/head", s.getHead)
	mux.HandleFunc("GET /v1/state/{height}", s.getState)
	mux.HandleFunc("GET /v1/state/{height}/accounts/{account}", s.getAccount)
	mux.HandleFunc("POST /v1/snapshots/build", s.buildSnapshot)
	mux.HandleFunc("GET /v1/snapshots", s.listSnapshots)
	mux.HandleFunc("POST /v1/prune", s.prune)
	s.handler = mux
	return s
}

// Handler returns the root http.Handler.
func (s *Server) Handler() http.Handler { return s.handler }

type errorBody struct {
	Error string `json:"error"`
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, err error) {
	code := http.StatusInternalServerError
	switch {
	case errors.Is(err, store.ErrReaderTimeout), errors.Is(err, context.DeadlineExceeded):
		code = http.StatusRequestTimeout
	case errors.Is(err, store.ErrHeightPruned):
		code = http.StatusGone
	case errors.Is(err, store.ErrHeightInFuture), errors.Is(err, store.ErrInvalidBlock):
		code = http.StatusBadRequest
	case errors.Is(err, context.Canceled):
		code = 499 // client closed request
	}
	writeJSON(w, code, errorBody{Error: err.Error()})
}

// readerTimeout parses ?timeout_ms=. Absent means "store default". An
// explicit value <= 0 maps to a nanosecond lease — i.e. fail immediately —
// so callers can deterministically exercise the reader-timeout path.
func readerTimeout(r *http.Request) time.Duration {
	v := r.URL.Query().Get("timeout_ms")
	if v == "" {
		return 0
	}
	ms, err := strconv.Atoi(v)
	if err != nil {
		return 0
	}
	if ms <= 0 {
		return time.Nanosecond
	}
	return time.Duration(ms) * time.Millisecond
}

func (s *Server) healthz(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

type postBlockRequest struct {
	Changes map[string]int64 `json:"changes"`
}

func (s *Server) postBlock(w http.ResponseWriter, r *http.Request) {
	var req postBlockRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, errorBody{Error: "invalid JSON: " + err.Error()})
		return
	}
	h, err := s.st.AppendBlock(req.Changes)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, map[string]uint64{"height": h})
}

func (s *Server) getHead(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]uint64{"height": s.st.Head()})
}

func (s *Server) getState(w http.ResponseWriter, r *http.Request) {
	height, err := strconv.ParseUint(r.PathValue("height"), 10, 64)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, errorBody{Error: "bad height"})
		return
	}
	sum, err := s.st.Summary(r.Context(), height, readerTimeout(r))
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, sum)
}

func (s *Server) getAccount(w http.ResponseWriter, r *http.Request) {
	height, err := strconv.ParseUint(r.PathValue("height"), 10, 64)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, errorBody{Error: "bad height"})
		return
	}
	acct := r.PathValue("account")
	bal, err := s.st.BalanceAt(r.Context(), height, acct, readerTimeout(r))
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"height": height, "account": acct, "balance": bal,
	})
}

type buildRequest struct {
	Height uint64 `json:"height"` // 0 = current head
}

func (s *Server) buildSnapshot(w http.ResponseWriter, r *http.Request) {
	var req buildRequest
	if r.Body != nil && r.ContentLength != 0 {
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			writeJSON(w, http.StatusBadRequest, errorBody{Error: "invalid JSON: " + err.Error()})
			return
		}
	}
	meta, err := s.st.BuildSnapshot(r.Context(), req.Height)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, meta)
}

func (s *Server) listSnapshots(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"snapshots": s.st.ListSnapshots()})
}

func (s *Server) prune(w http.ResponseWriter, r *http.Request) {
	if err := s.st.Prune(r.Context()); err != nil {
		writeError(w, fmt.Errorf("prune: %w", err))
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "pruned"})
}
