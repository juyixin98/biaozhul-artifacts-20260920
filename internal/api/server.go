// Package api exposes the promotion engine over HTTP using chi.
package api

import (
	"encoding/json"
	"errors"
	"net/http"
	"strconv"

	"github.com/example/artifact-promotion/internal/core"
	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"
)

type handler struct {
	svc *core.Service
}

func NewRouter(svc *core.Service) http.Handler {
	h := &handler{svc: svc}
	r := chi.NewRouter()
	r.Use(middleware.RequestID, middleware.RealIP, middleware.Recoverer)
	r.Route("/v1", func(r chi.Router) {
		r.Get("/healthz", func(w http.ResponseWriter, _ *http.Request) {
			writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
		})
		r.Post("/envs", h.createEnv)
		r.Get("/envs", h.listEnvs)
		r.Get("/envs/{env}", h.getEnv)
		r.Get("/envs/{env}/history", h.envHistory)
		r.Post("/envs/{env}/artifacts", h.ingest)
		r.Post("/evidence", h.addEvidence)
		r.Post("/policies", h.addPolicy)
		r.Post("/approvals", h.addApproval)
		r.Post("/promotions", h.promote)
		r.Post("/rollbacks", h.rollback)
		r.Get("/attempts", h.listAttempts)
		r.Get("/attempts/{id}", h.getAttempt)
	})
	return r
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

func writeErr(w http.ResponseWriter, status int, code, msg string) {
	writeJSON(w, status, map[string]any{"error": map[string]string{"code": code, "message": msg}})
}

// serviceErr maps infrastructure/domain errors to HTTP responses.
func serviceErr(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, core.ErrNotFound):
		writeErr(w, http.StatusNotFound, "not_found", err.Error())
	case errors.Is(err, core.ErrInvalid):
		writeErr(w, http.StatusBadRequest, "invalid_request", err.Error())
	default:
		writeErr(w, http.StatusInternalServerError, "internal", err.Error())
	}
}

// attemptResponse returns 201 for a succeeded attempt and 409 for a
// domain-rejected one; the attempt body (full evidence) is returned either
// way.
func attemptResponse(w http.ResponseWriter, a *core.Attempt, err error) {
	if err != nil {
		serviceErr(w, err)
		return
	}
	if a.Status == core.StatusFailed {
		writeJSON(w, http.StatusConflict, a)
		return
	}
	writeJSON(w, http.StatusCreated, a)
}

func decode(w http.ResponseWriter, r *http.Request, v any) bool {
	if err := json.NewDecoder(r.Body).Decode(v); err != nil {
		writeErr(w, http.StatusBadRequest, "bad_json", err.Error())
		return false
	}
	return true
}

func (h *handler) createEnv(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Name                string `json:"name"`
		RetentionKeep       int    `json:"retention_keep"`
		RetentionMaxAgeDays int    `json:"retention_max_age_days"`
	}
	if !decode(w, r, &req) {
		return
	}
	e, err := h.svc.CreateEnvironment(r.Context(), req.Name, req.RetentionKeep, req.RetentionMaxAgeDays)
	if err != nil {
		serviceErr(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, e)
}

func (h *handler) listEnvs(w http.ResponseWriter, r *http.Request) {
	envs, err := h.svc.ListEnvironments(r.Context())
	if err != nil {
		serviceErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, envs)
}

func (h *handler) getEnv(w http.ResponseWriter, r *http.Request) {
	e, err := h.svc.GetEnvironment(r.Context(), chi.URLParam(r, "env"))
	if err != nil {
		serviceErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, e)
}

func (h *handler) envHistory(w http.ResponseWriter, r *http.Request) {
	hist, err := h.svc.EnvHistory(r.Context(), chi.URLParam(r, "env"))
	if err != nil {
		serviceErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, hist)
}

// ingest accepts the raw artifact bytes and atomically moves the
// environment pointer to the computed digest. expected_generation is a
// required query parameter — the same optimistic guard as promotion.
func (h *handler) ingest(w http.ResponseWriter, r *http.Request) {
	env := chi.URLParam(r, "env")
	genStr := r.URL.Query().Get("expected_generation")
	if genStr == "" {
		writeErr(w, http.StatusBadRequest, "invalid_request", "expected_generation query parameter is required")
		return
	}
	gen, err := strconv.ParseInt(genStr, 10, 64)
	if err != nil {
		writeErr(w, http.StatusBadRequest, "invalid_request", "expected_generation must be an integer")
		return
	}
	defer r.Body.Close()
	a, err := h.svc.Ingest(r.Context(), env, r.Header.Get("Content-Type"), http.MaxBytesReader(w, r.Body, 1<<30), gen)
	attemptResponse(w, a, err)
}

func (h *handler) addEvidence(w http.ResponseWriter, r *http.Request) {
	var req struct {
		ID             string         `json:"id"`
		ArtifactDigest string         `json:"artifact_digest"`
		Suite          string         `json:"suite"`
		Passed         bool           `json:"passed"`
		Report         map[string]any `json:"report"`
	}
	if !decode(w, r, &req) {
		return
	}
	e, err := h.svc.AddEvidence(r.Context(), req.ID, req.ArtifactDigest, req.Suite, req.Passed, req.Report)
	if err != nil {
		serviceErr(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, e)
}

func (h *handler) addPolicy(w http.ResponseWriter, r *http.Request) {
	var req struct {
		ID                 string         `json:"id"`
		SourceEnv          string         `json:"source_env"`
		TargetEnv          string         `json:"target_env"`
		RequiredSuite      string         `json:"required_suite"`
		MinEvidenceVersion int            `json:"min_evidence_version"`
		Body               map[string]any `json:"body"`
	}
	if !decode(w, r, &req) {
		return
	}
	p, err := h.svc.AddPolicy(r.Context(), req.ID, req.SourceEnv, req.TargetEnv, req.RequiredSuite, req.MinEvidenceVersion, req.Body)
	if err != nil {
		serviceErr(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, p)
}

func (h *handler) addApproval(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Environment    string `json:"environment"`
		ArtifactDigest string `json:"artifact_digest"`
		Approver       string `json:"approver"`
	}
	if !decode(w, r, &req) {
		return
	}
	a, err := h.svc.AddApproval(r.Context(), req.Environment, req.ArtifactDigest, req.Approver)
	if err != nil {
		serviceErr(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, a)
}

func (h *handler) promote(w http.ResponseWriter, r *http.Request) {
	var req core.PromoteRequest
	if !decode(w, r, &req) {
		return
	}
	a, err := h.svc.Promote(r.Context(), req)
	attemptResponse(w, a, err)
}

func (h *handler) rollback(w http.ResponseWriter, r *http.Request) {
	var req core.RollbackRequest
	if !decode(w, r, &req) {
		return
	}
	a, err := h.svc.Rollback(r.Context(), req)
	attemptResponse(w, a, err)
}

func (h *handler) listAttempts(w http.ResponseWriter, r *http.Request) {
	attempts, err := h.svc.ListAttempts(r.Context(), r.URL.Query().Get("kind"), r.URL.Query().Get("status"))
	if err != nil {
		serviceErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, attempts)
}

func (h *handler) getAttempt(w http.ResponseWriter, r *http.Request) {
	a, err := h.svc.GetAttempt(r.Context(), chi.URLParam(r, "id"))
	if err != nil {
		serviceErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, a)
}
