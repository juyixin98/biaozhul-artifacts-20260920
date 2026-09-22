// Package api wires HTTP routes to services and enforces RBAC.
package api

import (
	"net/http"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"
	"github.com/jackc/pgx/v5/pgxpool"

	"dams/internal/platform/auth"
	"dams/internal/service/alerts"
	"dams/internal/service/auditchain"
	"dams/internal/service/detection"
	"dams/internal/service/ingest"
	"dams/internal/service/ruleadmin"
)

type Deps struct {
	Pool   *pgxpool.Pool
	Ingest *ingest.Service
	Rules  *ruleadmin.Service
	Alerts *alerts.Service
	Chain  *auditchain.Service
	Engine *detection.Engine
}

func NewRouter(d Deps) chi.Router {
	r := chi.NewRouter()
	r.Use(middleware.RealIP)
	r.Use(middleware.Recoverer)

	r.Get("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"status":"ok"}`))
	})

	r.Route("/v1", func(r chi.Router) {
		r.Use(auth.Middleware(d.Pool))
		h := &handlers{d: d}

		// Ingestion: collectors and admins.
		r.With(requireRole(auth.RoleCollector, auth.RoleAdmin)).
			Post("/events:batch", h.IngestBatch)

		// Rule configuration: admins only.
		r.With(requireRole(auth.RoleAdmin)).Route("/rules", func(r chi.Router) {
			r.Post("/", h.CreateRule)
			r.Get("/{ruleType}", h.ListRules)
		})

		// Alerts.
		r.Route("/alerts", func(r chi.Router) {
			r.With(requireRole(auth.RoleAnalyst, auth.RoleAdmin, auth.RoleAuditor)).
				Get("/", h.ListAlerts)
			r.With(requireRole(auth.RoleAnalyst, auth.RoleAdmin, auth.RoleAuditor)).
				Get("/{id}", h.GetAlert)
			r.With(requireRole(auth.RoleAnalyst, auth.RoleAdmin)).
				Post("/{id}:transition", h.TransitionAlert)
			r.With(requireRole(auth.RoleAnalyst, auth.RoleAdmin)).
				Post("/recompute", h.Recompute)
		})

		// Event export — masked for every role allowed to read it.
		// Collectors are write-only and cannot read.
		r.With(requireRole(auth.RoleAdmin, auth.RoleAnalyst, auth.RoleAuditor)).
			Get("/events", h.ListEvents)

		// Audit chain inspection — admin and auditor, read-only.
		r.Route("/audit", func(r chi.Router) {
			r.With(requireRole(auth.RoleAdmin, auth.RoleAuditor)).
				Get("/entries", h.ListAuditEntries)
			r.With(requireRole(auth.RoleAdmin, auth.RoleAuditor)).
				Get("/verify", h.VerifyChain)
		})
	})
	return r
}
