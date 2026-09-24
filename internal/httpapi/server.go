// Package httpapi exposes the behavior-tree backend over HTTP.
package httpapi

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"

	"github.com/google/uuid"

	"resumable-bt/internal/engine"
	"resumable-bt/internal/store"
)

// Server wires the engine to HTTP routes.
type Server struct {
	eng *engine.Engine
	mux *http.ServeMux
}

// New builds the routed server.
func New(eng *engine.Engine) *Server {
	s := &Server{eng: eng, mux: http.NewServeMux()}
	s.routes()
	return s
}

// Handler returns the root handler.
func (s *Server) Handler() http.Handler { return s.mux }

func (s *Server) routes() {
	s.mux.HandleFunc("GET /healthz", s.health)

	s.mux.HandleFunc("POST /trees/{name}/versions", s.publish)
	s.mux.HandleFunc("GET /trees/{name}/versions/{version}", s.getVersion)
	s.mux.HandleFunc("GET /trees/{name}/versions/latest", s.latestVersion)

	s.mux.HandleFunc("POST /executions", s.start)
	s.mux.HandleFunc("GET /executions/{id}", s.get)
	s.mux.HandleFunc("GET /executions/{id}/snapshot", s.snapshot)
	s.mux.HandleFunc("POST /executions/{id}/tick", s.tick)
	s.mux.HandleFunc("POST /executions/{id}/cancel", s.cancel)
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, status int, code, msg string) {
	writeJSON(w, status, map[string]string{"error": code, "message": msg})
}

func mapError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, store.ErrNotFound):
		writeErr(w, http.StatusNotFound, "not_found", err.Error())
	case errors.Is(err, store.ErrConflict):
		writeErr(w, http.StatusConflict, "conflict", err.Error())
	default:
		writeErr(w, http.StatusBadRequest, "invalid", err.Error())
	}
}

func (s *Server) health(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

type publishResponse struct {
	Name        string `json:"name"`
	Version     int64  `json:"version"`
	ContentHash string `json:"content_hash"`
	Created     bool   `json:"created"`
}

func (s *Server) publish(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("name")
	raw, err := io.ReadAll(io.LimitReader(r.Body, 1<<20))
	if err != nil {
		writeErr(w, http.StatusBadRequest, "read_error", err.Error())
		return
	}
	v, created, err := s.eng.Publish(r.Context(), name, raw)
	if err != nil {
		mapError(w, err)
		return
	}
	status := http.StatusOK
	if created {
		status = http.StatusCreated
	}
	writeJSON(w, status, publishResponse{
		Name: v.Name, Version: v.Version, ContentHash: v.ContentHash, Created: created,
	})
}

func (s *Server) getVersion(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("name")
	v, err := s.eng.GetVersion(r.Context(), name, r.PathValue("version"))
	if err != nil {
		mapError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, versionView{
		Name: v.Name, Version: v.Version, ContentHash: v.ContentHash,
		Definition: json.RawMessage(v.Definition), PublishedAt: v.PublishedAt,
	})
}

func (s *Server) latestVersion(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("name")
	n, err := s.eng.LatestVersion(r.Context(), name)
	if err != nil {
		mapError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"name": name, "version": itoa(n)})
}

type versionView struct {
	Name        string          `json:"name"`
	Version     int64           `json:"version"`
	ContentHash string          `json:"content_hash"`
	Definition  json.RawMessage `json:"definition"`
	PublishedAt any             `json:"published_at"`
}

type startRequest struct {
	Tree    string `json:"tree"`
	Version int64  `json:"version"`
}

type startResponse struct {
	ExecutionID string `json:"execution_id"`
	Tree        string `json:"tree"`
	Version     int64  `json:"version"`
	Status      string `json:"status"`
}

func (s *Server) start(w http.ResponseWriter, r *http.Request) {
	var req startRequest
	if err := json.NewDecoder(io.LimitReader(r.Body, 1<<20)).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid_json", err.Error())
		return
	}
	if req.Tree == "" {
		writeErr(w, http.StatusBadRequest, "invalid", "tree is required")
		return
	}
	id, ver, err := s.eng.StartExecution(r.Context(), req.Tree, req.Version)
	if err != nil {
		mapError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, startResponse{
		ExecutionID: id.String(), Tree: ver.Name, Version: ver.Version, Status: "running",
	})
}

func parseID(w http.ResponseWriter, r *http.Request) (uuid.UUID, bool) {
	id, err := uuid.Parse(r.PathValue("id"))
	if err != nil {
		writeErr(w, http.StatusBadRequest, "invalid_id", "execution id must be a UUID")
		return uuid.Nil, false
	}
	return id, true
}

func (s *Server) get(w http.ResponseWriter, r *http.Request) {
	id, ok := parseID(w, r)
	if !ok {
		return
	}
	snap, err := s.eng.Snapshot(r.Context(), id)
	if err != nil {
		mapError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"execution_id": snap.Execution.ID,
		"tree":         snap.Execution.TreeName,
		"version":      snap.Execution.TreeVersion,
		"content_hash": snap.Execution.ContentHash,
		"status":       snap.Execution.Status,
		"next_tick":    snap.Execution.NextTick,
		"created_at":   snap.Execution.CreatedAt,
		"finalized_at": snap.Execution.FinalizedAt,
		"next_due_ms":  snap.NextDueUnixMS,
		"ticks":        snap.Ticks,
	})
}

func (s *Server) snapshot(w http.ResponseWriter, r *http.Request) {
	id, ok := parseID(w, r)
	if !ok {
		return
	}
	snap, err := s.eng.Snapshot(r.Context(), id)
	if err != nil {
		mapError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, snap)
}

func (s *Server) tick(w http.ResponseWriter, r *http.Request) {
	id, ok := parseID(w, r)
	if !ok {
		return
	}
	note := ""
	if r.Body != nil {
		var body struct {
			Note string `json:"note"`
		}
		_ = json.NewDecoder(io.LimitReader(r.Body, 1<<20)).Decode(&body)
		note = body.Note
	}
	res, err := s.eng.Tick(r.Context(), id, note)
	if err != nil {
		mapError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, res)
}

func (s *Server) cancel(w http.ResponseWriter, r *http.Request) {
	id, ok := parseID(w, r)
	if !ok {
		return
	}
	if err := s.eng.Cancel(r.Context(), id); err != nil {
		mapError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"execution_id": id.String(), "status": "canceled"})
}
