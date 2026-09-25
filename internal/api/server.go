// Package api exposes the build service over a small JSON HTTP API.
package api

import (
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"strconv"

	"reprobuild/internal/builder"
)

// Server wires the builder service to HTTP handlers.
type Server struct {
	svc    *builder.Service
	logger *slog.Logger
}

// NewServer creates the API server.
func NewServer(svc *builder.Service, logger *slog.Logger) *Server {
	if logger == nil {
		logger = slog.Default()
	}
	return &Server{svc: svc, logger: logger}
}

// Routes returns a mux with method-constrained routes (Go 1.22 pattern mux).
func (s *Server) Routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /api/v1/healthz", s.health)
	mux.HandleFunc("POST /api/v1/builds", s.createBuild)
	mux.HandleFunc("GET /api/v1/builds/{id}", s.getBuild)
	mux.HandleFunc("GET /api/v1/builds/{id}/artifact", s.getArtifact)
	return mux
}

func (s *Server) health(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *Server) createBuild(w http.ResponseWriter, r *http.Request) {
	var req builder.BuildRequest
	dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4<<20))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON request: "+err.Error())
		return
	}
	if req.SourceDir == "" && len(req.Files) == 0 {
		writeError(w, http.StatusBadRequest, "source_dir or files is required")
		return
	}
	for i, fx := range req.Fixtures {
		if len(fx.Args) == 0 {
			writeError(w, http.StatusBadRequest, "fixtures["+strconv.Itoa(i)+"].args must be non-empty")
			return
		}
	}
	rec := s.svc.Build(r.Context(), req)
	s.logger.Info("build finished", "id", rec.ID, "status", rec.Status)
	writeJSON(w, http.StatusOK, rec)
}

func (s *Server) getBuild(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	rec, ok := s.svc.Get(id)
	if !ok {
		writeError(w, http.StatusNotFound, "build not found: "+id)
		return
	}
	writeJSON(w, http.StatusOK, rec)
}

func (s *Server) getArtifact(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	f, art, err := s.svc.OpenArtifact(id)
	if err != nil {
		if errors.Is(err, builder.ErrNotFound) {
			writeError(w, http.StatusNotFound, "build not found: "+id)
			return
		}
		writeError(w, http.StatusConflict, err.Error())
		return
	}
	defer f.Close()
	w.Header().Set("Content-Type", "application/x-tar")
	w.Header().Set("Content-Length", strconv.FormatInt(art.Size, 10))
	w.Header().Set("X-Artifact-Sha256", art.Sha256)
	w.Header().Set("X-Artifact-Entries", strconv.Itoa(art.Entries))
	if _, err := io.Copy(w, f); err != nil {
		s.logger.Warn("artifact stream failed", "id", id, "err", err)
	}
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]any{"error": msg, "status": status})
}
