package api

import (
	"net/http"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"
	"github.com/jackc/pgx/v5/pgxpool"

	"sircc/internal/auth"
	"sircc/internal/service"
)

type Handlers struct {
	svc *service.Service
}

func NewRouter(pool *pgxpool.Pool, svc *service.Service) http.Handler {
	h := &Handlers{svc: svc}
	r := chi.NewRouter()
	r.Use(middleware.Recoverer)
	r.Use(middleware.RequestID)
	r.Use(middleware.RealIP)

	r.Get("/healthz", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})

	r.Route("/api/v1", func(r chi.Router) {
		r.Use(auth.Middleware(pool))

		r.Get("/users", h.listUsers)

		r.Route("/incidents", func(r chi.Router) {
			r.Post("/", h.createIncident)
			r.Get("/", h.listIncidents)

			r.Route("/{incidentID}", func(r chi.Router) {
				r.Get("/", h.getIncident)
				r.Post("/transitions", h.transition)
				r.Get("/export", h.exportIncident)

				r.Get("/members", h.listMembers)
				r.Put("/members/{userID}", h.assignMember)

				r.Get("/evidence", h.listEvidence)
				r.Post("/evidence", h.addEvidence)
				r.Post("/evidence/{evidenceID}/notes", h.addEvidenceNote)

				r.Get("/action-items", h.listActionItems)
				r.Post("/action-items", h.createActionItem)
				r.Post("/action-items/{itemID}/reschedule", h.rescheduleActionItem)
				r.Post("/action-items/{itemID}/status", h.setActionItemStatus)
			})
		})
	})
	return r
}

func (h *Handlers) listUsers(w http.ResponseWriter, r *http.Request) {
	u := auth.User(r.Context())
	// User directory is visible to any authenticated caller so admins can
	// resolve ids when assigning people.
	users, err := h.svc.ListUsers(r.Context(), u)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"users": users})
}

var _ = time.RFC3339
