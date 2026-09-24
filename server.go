package main

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
)

// Server exposes the vector-clock register over HTTP. It contains only
// net/http: no third-party router or web framework is used.
type Server struct {
	store *Store
	mux   *http.ServeMux
}

// NewServer wires routes to store operations.
func NewServer(store *Store) *Server {
	srv := &Server{store: store, mux: http.NewServeMux()}

	srv.mux.HandleFunc("GET /health", srv.handleHealth)
	srv.mux.HandleFunc("GET /replicas", srv.handleListReplicas)
	srv.mux.HandleFunc("POST /replicas/reset", srv.handleReset)
	srv.mux.HandleFunc("POST /replicas", srv.handleCreateReplica)
	srv.mux.HandleFunc("DELETE /replicas/{id}", srv.handleDeleteReplica)
	srv.mux.HandleFunc("GET /replicas/{id}", srv.handleReplicaSnapshot)
	srv.mux.HandleFunc("POST /replicas/{id}/sync", srv.handleSync)
	srv.mux.HandleFunc("POST /replicas/{id}/keys/{key}/messages", srv.handleDeliver)
	srv.mux.HandleFunc("GET /replicas/{id}/keys/{key}", srv.handleRead)
	srv.mux.HandleFunc("PUT /replicas/{id}/keys/{key}", srv.handleWrite)
	srv.mux.HandleFunc("POST /replicas/{id}/keys/{key}/merge", srv.handleMerge)

	return srv
}

func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	s.mux.ServeHTTP(w, r)
}

// ------------------------------------------------------------------ payloads

type createReplicaRequest struct {
	ID string `json:"id"`
}

type writeRequest struct {
	Value string `json:"value"`
}

type mergeRequest struct {
	Value   string   `json:"value"`
	Context []string `json:"context"`
}

type deliverRequest struct {
	Versions []Version `json:"versions"`
}

type syncRequest struct {
	From string `json:"from"`
	To   string `json:"to"`
	Mode string `json:"mode"`
}

// ------------------------------------------------------------------ helpers

// writeJSON serializes v as JSON. Empty slices/maps render as []/{} via the
// store's initialized values; callers pass non-nil collections for that.
func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}

// decodeJSON is a strict decoder: unknown fields or trailing data are rejected
// so that mistyped requests fail loudly instead of being silently ignored.
func decodeJSON(r *http.Request, dst any) error {
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(dst); err != nil {
		return err
	}
	if err := dec.Decode(&struct{}{}); err != io.EOF {
		return errors.New("request body must contain a single JSON value")
	}
	return nil
}

// statusFor maps domain errors onto HTTP status codes.
func statusFor(err error) int {
	switch {
	case errors.Is(err, ErrReplicaNotFound), errors.Is(err, ErrKeyNotFound):
		return http.StatusNotFound
	case errors.Is(err, ErrReplicaExists):
		return http.StatusConflict
	case errors.Is(err, ErrSameReplica):
		return http.StatusBadRequest
	default:
		return http.StatusBadRequest
	}
}

// ------------------------------------------------------------------ handlers

func (s *Server) handleHealth(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *Server) handleReset(w http.ResponseWriter, _ *http.Request) {
	s.store.Reset()
	writeJSON(w, http.StatusOK, map[string]string{"status": "reset"})
}

func (s *Server) handleListReplicas(w http.ResponseWriter, _ *http.Request) {
	ids := s.store.ReplicaIDs()
	if ids == nil {
		ids = []string{}
	}
	writeJSON(w, http.StatusOK, map[string]any{"replicas": ids})
}

func (s *Server) handleCreateReplica(w http.ResponseWriter, r *http.Request) {
	var req createReplicaRequest
	if err := decodeJSON(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
		return
	}
	if err := s.store.CreateReplica(req.ID); err != nil {
		writeError(w, statusFor(err), err.Error())
		return
	}
	writeJSON(w, http.StatusCreated, map[string]string{"id": req.ID, "status": "created"})
}

func (s *Server) handleDeleteReplica(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if err := s.store.DeleteReplica(id); err != nil {
		writeError(w, statusFor(err), err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"id": id, "status": "deleted"})
}

func (s *Server) handleReplicaSnapshot(w http.ResponseWriter, r *http.Request) {
	snap, err := s.store.Snapshot(r.PathValue("id"))
	if err != nil {
		writeError(w, statusFor(err), err.Error())
		return
	}
	writeJSON(w, http.StatusOK, snap)
}

func (s *Server) handleRead(w http.ResponseWriter, r *http.Request) {
	view, err := s.store.Read(r.PathValue("id"), r.PathValue("key"))
	if err != nil {
		writeError(w, statusFor(err), err.Error())
		return
	}
	writeJSON(w, http.StatusOK, view)
}

func (s *Server) handleWrite(w http.ResponseWriter, r *http.Request) {
	var req writeRequest
	if err := decodeJSON(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
		return
	}
	v, survivors, err := s.store.Write(r.PathValue("id"), r.PathValue("key"), req.Value)
	if err != nil {
		writeError(w, statusFor(err), err.Error())
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{
		"written":   v,
		"survivors": survivors,
	})
}

func (s *Server) handleMerge(w http.ResponseWriter, r *http.Request) {
	var req mergeRequest
	if err := decodeJSON(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
		return
	}
	if len(req.Context) == 0 {
		writeError(w, http.StatusBadRequest, "merge requires a non-empty \"context\" array of version ids; use PUT to overwrite all siblings")
		return
	}
	v, survivors, err := s.store.Merge(r.PathValue("id"), r.PathValue("key"), req.Value, req.Context)
	if err != nil {
		writeError(w, statusFor(err), err.Error())
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{
		"merged":    v,
		"context":   req.Context,
		"survivors": survivors,
	})
}

func (s *Server) handleDeliver(w http.ResponseWriter, r *http.Request) {
	var req deliverRequest
	if err := decodeJSON(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
		return
	}
	if len(req.Versions) == 0 {
		writeError(w, http.StatusBadRequest, "\"versions\" must contain at least one version")
		return
	}
	result, err := s.store.Deliver(r.PathValue("id"), r.PathValue("key"), req.Versions)
	if err != nil {
		writeError(w, statusFor(err), err.Error())
		return
	}
	writeJSON(w, http.StatusOK, result)
}

func (s *Server) handleSync(w http.ResponseWriter, r *http.Request) {
	var req syncRequest
	if err := decodeJSON(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
		return
	}
	if req.From == "" || req.To == "" {
		writeError(w, http.StatusBadRequest, "both \"from\" and \"to\" are required")
		return
	}
	if req.Mode == "" {
		req.Mode = SyncModeOneWay
	}
	if req.Mode != SyncModeOneWay && req.Mode != SyncModeTwoWay {
		writeError(w, http.StatusBadRequest, `mode must be "one-way" or "two-way"`)
		return
	}
	// The replica named in the path is informational: the body fully
	// identifies the source and destination. This lets a client drive a sync
	// through either peer's URL.
	if req.From != r.PathValue("id") && req.To != r.PathValue("id") {
		writeError(w, http.StatusBadRequest, "the replica in the URL must be one of from/to")
		return
	}
	report, err := s.store.Sync(req.From, req.To, req.Mode)
	if err != nil {
		writeError(w, statusFor(err), err.Error())
		return
	}
	writeJSON(w, http.StatusOK, report)
}
