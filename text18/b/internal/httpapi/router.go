package httpapi

import (
	"net/http"

	"github.com/go-chi/chi/v5"

	"sircc/internal/db"
	"sircc/internal/httpx"
	"sircc/internal/middleware"
	"sircc/internal/service"
)

// Router builds the complete HTTP API. All mutating routes require
// X-User-Id (auth) and X-Request-Id (idempotency).
func Router(svc *service.Service, q db.Querier) http.Handler {
	r := chi.NewRouter()

	r.Use(requestLogger)
	r.Use(recoverer)

	r.Get("/healthz", func(w http.ResponseWriter, r *http.Request) {
		httpx.JSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})

	r.Group(func(r chi.Router) {
		r.Use(middleware.Authenticate(q))

		// Read-only catalog
		r.Get("/v1/users", func(w http.ResponseWriter, r *http.Request) {
			users, err := q.ListUsers(r.Context())
			if err != nil {
				httpx.ErrorJSON(w, http.StatusInternalServerError, httpx.CodeInternal, "list users")
				return
			}
			httpx.JSON(w, http.StatusOK, map[string]any{"users": users})
		})

		r.Route("/v1/incidents", func(r chi.Router) {
			r.Post("/", svc.CreateIncident)
			r.Get("/", svc.ListIncidents)

			r.Route("/{incidentID}", func(r chi.Router) {
				r.Get("/", svc.GetIncident)
				r.Post("/transition", svc.Transition)
				r.Post("/assignments", svc.AssignPerson)
				r.Get("/events", svc.ListStageEvents)
				r.Get("/export", svc.ExportIncident)

				r.Route("/evidence", func(r chi.Router) {
					r.Post("/", svc.AddEvidence)
					r.Get("/", svc.ListEvidence)
					r.Post("/{evidenceID}/notes", svc.AddEvidenceNote)
				})

				r.Route("/action-items", func(r chi.Router) {
					r.Post("/", svc.CreateActionItem)
					r.Get("/", svc.ListActionItems)
				})
			})
		})

		r.Route("/v1/action-items/{itemID}", func(r chi.Router) {
			r.Post("/reschedule", svc.RescheduleActionItem)
			r.Post("/status", svc.UpdateActionItemStatus)
		})

		r.Get("/v1/notifications", svc.ListNotifications)
		r.Get("/v1/audit-events", svc.ListAuditEvents)
	})

	return r
}
