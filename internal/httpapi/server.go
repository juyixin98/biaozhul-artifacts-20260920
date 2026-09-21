// Package httpapi exposes the SIRCC JSON API over chi.
//
// Authentication is header-based (no external identity service):
//   X-User-ID:   arbitrary user identifier (required)
//   X-User-Role: analyst | responder | admin   (required)
package httpapi

import (
	"context"
	"encoding/json"
	"net/http"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"
	"github.com/google/uuid"

	"sircc/internal/incident"
)

type ctxKey int

const (
	ctxUser ctxKey = iota
	ctxRole
)

// Server wires the incident service to HTTP handlers.
type Server struct {
	svc *incident.Service
}

func NewRouter(svc *incident.Service) http.Handler {
	s := &Server{svc: svc}
	r := chi.NewRouter()
	r.Use(middleware.Recoverer)
	r.Use(authMiddleware)

	r.Get("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})

	r.Route("/incidents", func(r chi.Router) {
		r.Post("/", s.createIncident)
		r.Get("/", s.listIncidents)
		r.Route("/{incidentID}", func(r chi.Router) {
			r.Get("/", s.getIncident)
			r.Post("/members", s.assignMember)
			r.Get("/members", s.listMembers)
			r.Post("/transitions", s.transition)
			r.Put("/postmortem", s.setPostmortem)
			r.Post("/evidence", s.addEvidence)
			r.Post("/action-items", s.createActionItem)
			r.Get("/action-items", s.listActionItems)
			r.Get("/audit", s.listAudit)
			r.Get("/reminders", s.listReminders)
			r.Get("/export", s.exportIncident)
		})
	})
	r.Post("/evidence/{evidenceID}/notes", s.addEvidenceNote)
	r.Post("/action-items/{itemID}/reschedule", s.rescheduleActionItem)
	return r
}

// --- helpers -----------------------------------------------------------------

func authMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		user := r.Header.Get("X-User-ID")
		role := r.Header.Get("X-User-Role")
		if user == "" {
			writeErr(w, http.StatusUnauthorized, "UNAUTHENTICATED", "X-User-ID header is required")
			return
		}
		switch role {
		case "analyst", "responder", "admin":
		default:
			writeErr(w, http.StatusUnauthorized, "UNAUTHENTICATED", "X-User-Role must be analyst, responder or admin")
			return
		}
		ctx := context.WithValue(r.Context(), ctxUser, user)
		ctx = context.WithValue(ctx, ctxRole, role)
		next.ServeHTTP(w, r.WithContext(ctx))
	})
}

func actor(r *http.Request) string { return r.Context().Value(ctxUser).(string) }
func role(r *http.Request) string  { return r.Context().Value(ctxRole).(string) }

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, status int, code, msg string) {
	writeJSON(w, status, map[string]any{"error": map[string]string{"code": code, "message": msg}})
}

// writeSvc maps a domain error (or nil) to an HTTP response; returns true if
// an error was written.
func writeSvc(w http.ResponseWriter, err *incident.Error) bool {
	if err == nil {
		return false
	}
	writeErr(w, err.HTTP, err.Code, err.Message)
	return true
}

func decode(w http.ResponseWriter, r *http.Request, v any) bool {
	if err := json.NewDecoder(r.Body).Decode(v); err != nil {
		writeErr(w, http.StatusBadRequest, "BAD_JSON", "request body is not valid JSON: "+err.Error())
		return false
	}
	return true
}

func urlUUID(w http.ResponseWriter, r *http.Request, param string) (uuid.UUID, bool) {
	id, err := uuid.Parse(chi.URLParam(r, param))
	if err != nil {
		writeErr(w, http.StatusBadRequest, "BAD_ID", param+" is not a valid UUID")
		return uuid.Nil, false
	}
	return id, true
}

func parseTime(s string) (time.Time, error) {
	return time.Parse(time.RFC3339, s)
}

// --- handlers ------------------------------------------------------------------

func (s *Server) createIncident(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Title    string `json:"title"`
		Severity string `json:"severity"`
	}
	if !decode(w, r, &body) {
		return
	}
	inc, err := s.svc.CreateIncident(r.Context(), actor(r), body.Title, body.Severity)
	if writeSvc(w, err) {
		return
	}
	writeJSON(w, http.StatusCreated, inc)
}

func (s *Server) listIncidents(w http.ResponseWriter, r *http.Request) {
	incs, err := s.svc.ListIncidents(r.Context())
	if writeSvc(w, err) {
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"incidents": incs})
}

func (s *Server) getIncident(w http.ResponseWriter, r *http.Request) {
	id, ok := urlUUID(w, r, "incidentID")
	if !ok {
		return
	}
	view, err := s.svc.GetIncident(r.Context(), id)
	if writeSvc(w, err) {
		return
	}
	writeJSON(w, http.StatusOK, view)
}

func (s *Server) assignMember(w http.ResponseWriter, r *http.Request) {
	id, ok := urlUUID(w, r, "incidentID")
	if !ok {
		return
	}
	var body struct {
		UserID string `json:"user_id"`
		Role   string `json:"role"`
	}
	if !decode(w, r, &body) {
		return
	}
	if writeSvc(w, s.svc.AssignMember(r.Context(), actor(r), role(r), id, body.UserID, body.Role)) {
		return
	}
	writeJSON(w, http.StatusCreated, map[string]string{"status": "assigned"})
}

func (s *Server) listMembers(w http.ResponseWriter, r *http.Request) {
	id, ok := urlUUID(w, r, "incidentID")
	if !ok {
		return
	}
	ms, err := s.svc.ListMembers(r.Context(), id)
	if writeSvc(w, err) {
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"members": ms})
}

func (s *Server) transition(w http.ResponseWriter, r *http.Request) {
	id, ok := urlUUID(w, r, "incidentID")
	if !ok {
		return
	}
	var body struct {
		To              string `json:"to"`
		ExpectedVersion int32  `json:"expected_version"`
		RequestID       string `json:"request_id"`
	}
	if !decode(w, r, &body) {
		return
	}
	res, err := s.svc.Transition(r.Context(), incident.TransitionInput{
		IncidentID: id, To: body.To, ExpectedVersion: body.ExpectedVersion,
		RequestID: body.RequestID, Actor: actor(r),
	})
	if writeSvc(w, err) {
		return
	}
	writeJSON(w, http.StatusOK, res)
}

func (s *Server) setPostmortem(w http.ResponseWriter, r *http.Request) {
	id, ok := urlUUID(w, r, "incidentID")
	if !ok {
		return
	}
	var body struct {
		RootCause      string `json:"root_cause"`
		LessonsLearned string `json:"lessons_learned"`
	}
	if !decode(w, r, &body) {
		return
	}
	inc, err := s.svc.SetPostmortem(r.Context(), actor(r), id, body.RootCause, body.LessonsLearned)
	if writeSvc(w, err) {
		return
	}
	writeJSON(w, http.StatusOK, inc)
}

func (s *Server) addEvidence(w http.ResponseWriter, r *http.Request) {
	id, ok := urlUUID(w, r, "incidentID")
	if !ok {
		return
	}
	var body struct {
		Content string `json:"content"`
	}
	if !decode(w, r, &body) {
		return
	}
	ev, err := s.svc.AddEvidence(r.Context(), actor(r), id, body.Content)
	if writeSvc(w, err) {
		return
	}
	writeJSON(w, http.StatusCreated, ev)
}

func (s *Server) addEvidenceNote(w http.ResponseWriter, r *http.Request) {
	id, ok := urlUUID(w, r, "evidenceID")
	if !ok {
		return
	}
	var body struct {
		Note string `json:"note"`
	}
	if !decode(w, r, &body) {
		return
	}
	n, err := s.svc.AddEvidenceNote(r.Context(), actor(r), id, body.Note)
	if writeSvc(w, err) {
		return
	}
	writeJSON(w, http.StatusCreated, n)
}

func (s *Server) createActionItem(w http.ResponseWriter, r *http.Request) {
	id, ok := urlUUID(w, r, "incidentID")
	if !ok {
		return
	}
	var body struct {
		Title   string `json:"title"`
		OwnerID string `json:"owner_id"`
		DueAt   string `json:"due_at"`
	}
	if !decode(w, r, &body) {
		return
	}
	due, perr := parseTime(body.DueAt)
	if perr != nil {
		writeErr(w, http.StatusBadRequest, "VALIDATION", "due_at must be RFC3339")
		return
	}
	item, err := s.svc.CreateActionItem(r.Context(), actor(r), id, body.Title, body.OwnerID, due)
	if writeSvc(w, err) {
		return
	}
	writeJSON(w, http.StatusCreated, item)
}

func (s *Server) listActionItems(w http.ResponseWriter, r *http.Request) {
	id, ok := urlUUID(w, r, "incidentID")
	if !ok {
		return
	}
	items, err := s.svc.ListActionItems(r.Context(), id)
	if writeSvc(w, err) {
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"action_items": items})
}

func (s *Server) rescheduleActionItem(w http.ResponseWriter, r *http.Request) {
	id, ok := urlUUID(w, r, "itemID")
	if !ok {
		return
	}
	var body struct {
		DueAt string `json:"due_at"`
	}
	if !decode(w, r, &body) {
		return
	}
	due, perr := parseTime(body.DueAt)
	if perr != nil {
		writeErr(w, http.StatusBadRequest, "VALIDATION", "due_at must be RFC3339")
		return
	}
	item, err := s.svc.RescheduleActionItem(r.Context(), actor(r), id, due)
	if writeSvc(w, err) {
		return
	}
	writeJSON(w, http.StatusOK, item)
}

func (s *Server) listAudit(w http.ResponseWriter, r *http.Request) {
	id, ok := urlUUID(w, r, "incidentID")
	if !ok {
		return
	}
	evs, err := s.svc.ListAudit(r.Context(), id)
	if writeSvc(w, err) {
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"audit_events": evs})
}

func (s *Server) listReminders(w http.ResponseWriter, r *http.Request) {
	id, ok := urlUUID(w, r, "incidentID")
	if !ok {
		return
	}
	rs, err := s.svc.ListReminders(r.Context(), id)
	if writeSvc(w, err) {
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"reminders": rs})
}

func (s *Server) exportIncident(w http.ResponseWriter, r *http.Request) {
	id, ok := urlUUID(w, r, "incidentID")
	if !ok {
		return
	}
	exp, err := s.svc.Export(r.Context(), id)
	if writeSvc(w, err) {
		return
	}
	writeJSON(w, http.StatusOK, exp)
}
