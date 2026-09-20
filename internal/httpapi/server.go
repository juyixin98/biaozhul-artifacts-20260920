// Package httpapi exposes the SIRCC incident-response REST API.
//
// Authentication is header-based (X-User-Id) against the seeded users table;
// no external identity service is contacted. Authorization is role-based:
// analysts triage and handle evidence, the assigned responder drives the
// response phases, and admins assign personnel.
package httpapi

import (
	"context"
	"errors"
	"net/http"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"sircc/internal/store"
)

type ctxKey string

const ctxUser ctxKey = "user"

type Server struct {
	pool *pgxpool.Pool
	q    *store.Queries
}

func NewServer(pool *pgxpool.Pool) *Server {
	return &Server{pool: pool, q: store.New(pool)}
}

func (s *Server) Router() chi.Router {
	r := chi.NewRouter()
	r.Use(middleware.Recoverer)
	r.Get("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})

	r.Group(func(r chi.Router) {
		r.Use(s.authMiddleware)

		r.Post("/incidents", s.handleCreateIncident)
		r.Get("/incidents", s.handleListIncidents)
		r.Get("/incidents/{incidentID}", s.handleGetIncident)
		r.Post("/incidents/{incidentID}/assign", s.handleAssign)
		r.Post("/incidents/{incidentID}/transitions", s.handleTransition)
		r.Put("/incidents/{incidentID}/postmortem", s.handleUpdatePostmortem)
		r.Get("/incidents/{incidentID}/audit", s.handleListAudit)

		r.Post("/incidents/{incidentID}/evidence", s.handleAddEvidence)
		r.Get("/incidents/{incidentID}/evidence", s.handleListEvidence)
		r.Post("/evidence/{evidenceID}/notes", s.handleAddEvidenceNote)

		r.Post("/incidents/{incidentID}/action-items", s.handleCreateActionItem)
		r.Get("/incidents/{incidentID}/action-items", s.handleListActionItems)
		r.Post("/action-items/{actionItemID}/reschedule", s.handleRescheduleActionItem)
		r.Post("/action-items/{actionItemID}/complete", s.handleCompleteActionItem)

		r.Get("/incidents/{incidentID}/export", s.handleExport)
	})
	return r
}

// authMiddleware resolves the X-User-Id header to a users row.
func (s *Server) authMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw := r.Header.Get("X-User-Id")
		id, err := uuid.Parse(raw)
		if err != nil {
			writeErr(w, errUnauthenticated)
			return
		}
		u, err := s.q.GetUser(r.Context(), pgUUID(id))
		if err != nil {
			writeErr(w, errUnauthenticated)
			return
		}
		next.ServeHTTP(w, r.WithContext(context.WithValue(r.Context(), ctxUser, u)))
	})
}

func actor(r *http.Request) store.User {
	return r.Context().Value(ctxUser).(store.User)
}

func urlUUID(r *http.Request, param string) (uuid.UUID, *apiError) {
	return parseUUID(chi.URLParam(r, param))
}

// isNotFound maps pgx.ErrNoRows to the 404 API error.
func isNotFound(err error) bool { return errors.Is(err, pgx.ErrNoRows) }

// requireRole rejects callers whose role is not in the allowed set.
func requireRole(u store.User, roles ...string) *apiError {
	for _, role := range roles {
		if u.Role == role {
			return nil
		}
	}
	return errForbidden
}

// requireAssignee enforces case scope: the caller must be the responder
// currently assigned to the incident.
func requireAssignee(u store.User, inc store.Incident) *apiError {
	if u.Role != "responder" || !inc.AssigneeID.Valid || inc.AssigneeID != u.ID {
		return errForbidden
	}
	return nil
}
