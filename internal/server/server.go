// Package server wires the DAMS HTTP API: Chi router, authentication
// middleware, ingestion with detection, alert lifecycle, audit chain and
// role-based export.
package server

import (
	"encoding/json"
	"errors"
	"net/http"

	"github.com/go-chi/chi/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"dams.local/dams/internal/auth"
	sqlcgen "dams.local/dams/internal/db/sqlc"
)

type Server struct {
	pool *pgxpool.Pool
	q    *sqlcgen.Queries
}

func New(pool *pgxpool.Pool) *Server {
	return &Server{pool: pool, q: sqlcgen.New(pool)}
}

func (s *Server) Router() http.Handler {
	r := chi.NewRouter()
	r.Use(recoverer)
	r.Use(requestLogger)

	r.Get("/healthz", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})

	r.Route("/api/v1", func(r chi.Router) {
		r.Use(s.authenticate)

		// Caller's own memberships.
		r.Get("/orgs", s.handleListOrgs)

		// Every per-organization route resolves the org and enforces
		// membership before any handler runs.
		r.Route("/orgs/{org}", func(r chi.Router) {
			r.Use(s.resolveOrgMiddleware)

			// Ingestion (admin only)
			r.With(s.requireRole(auth.RoleAdmin)).
				Post("/events:batch", s.handleBatchEvents)
			r.With(s.requireRole(auth.RoleAdmin)).
				Get("/sources", s.handleListSources)

			// Rules: any member may read, only admins configure.
			r.Get("/rules", s.handleListRules)
			r.Get("/rules/{id}/versions", s.handleListRuleVersions)
			r.With(s.requireRole(auth.RoleAdmin)).
				Post("/rules", s.handleCreateRule)

			// Alerts: members may read; analysts/admins act.
			r.Get("/alerts", s.handleListAlerts)
			r.Get("/alerts/{id}", s.handleGetAlert)
			r.Get("/alerts/{id}/revisions", s.handleListRevisions)
			r.With(s.requireRole(auth.RoleAdmin, auth.RoleAnalyst)).
				Post("/alerts/{id}:transition", s.handleTransitionAlert)
			r.With(s.requireRole(auth.RoleAdmin, auth.RoleAnalyst)).
				Post("/alerts/{id}:assign", s.handleAssignAlert)
			r.With(s.requireRole(auth.RoleAdmin, auth.RoleAnalyst)).
				Post("/alerts/{id}:recompute", s.handleRecomputeAlert)

			// Audit chain: admin or auditor only (enforced in handler so
			// analysts get an explicit 403 rather than a missing route).
			r.Get("/audit", s.handleListAudit)
			r.Get("/audit/verify", s.handleVerifyAudit)

			// Export: all roles, masked; policy changes admin only.
			r.Get("/events:export", s.handleExport)
			r.With(s.requireRole(auth.RoleAdmin)).
				Put("/export-policy", s.handleUpdateExportPolicy)
		})
	})

	return r
}

// ---- helpers --------------------------------------------------------------

type apiError struct {
	Error   string `json:"error"`
	Code    string `json:"code,omitempty"`
	Details any    `json:"details,omitempty"`
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	_ = enc.Encode(v)
}

func writeError(w http.ResponseWriter, status int, code, msg string, details any) {
	writeJSON(w, status, apiError{Error: msg, Code: code, Details: details})
}

func decodeJSON(w http.ResponseWriter, r *http.Request, dst any, maxBytes int64) error {
	if maxBytes > 0 {
		r.Body = http.MaxBytesReader(w, r.Body, maxBytes)
	}
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(dst); err != nil {
		return err
	}
	if dec.More() {
		return errors.New("unexpected trailing JSON value")
	}
	return nil
}
