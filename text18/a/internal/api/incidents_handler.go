package api

import (
	"net/http"
	"strconv"

	"github.com/go-chi/chi/v5"
	"github.com/google/uuid"

	"sircc/internal/auth"
	"sircc/internal/domain"
	"sircc/internal/service"
)

type createIncidentRequest struct {
	Title       string `json:"title"`
	Description string `json:"description"`
	Severity    string `json:"severity"`
}

func (h *Handlers) createIncident(w http.ResponseWriter, r *http.Request) {
	var req createIncidentRequest
	if !decode(w, r, &req) {
		return
	}
	res, err := h.svc.CreateIncident(r.Context(), auth.User(r.Context()), service.CreateIncidentInput{
		Title:       req.Title,
		Description: req.Description,
		Severity:    req.Severity,
		RequestID:   requestID(r),
	})
	if err != nil {
		writeError(w, err)
		return
	}
	renderResult(w, res)
}

func (h *Handlers) listIncidents(w http.ResponseWriter, r *http.Request) {
	limit, _ := strconv.Atoi(r.URL.Query().Get("limit"))
	offset, _ := strconv.Atoi(r.URL.Query().Get("offset"))
	incidents, n, err := h.svc.ListIncidents(r.Context(), auth.User(r.Context()),
		r.URL.Query().Get("status"), limit, offset)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"incidents": incidents, "count": n})
}

func (h *Handlers) getIncident(w http.ResponseWriter, r *http.Request) {
	id, ok := parseUUID(w, chi.URLParam(r, "incidentID"))
	if !ok {
		return
	}
	detail, err := h.svc.GetIncident(r.Context(), auth.User(r.Context()), id, true)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, detail)
}

type transitionRequest struct {
	Action          string `json:"action"`
	ExpectedVersion int64  `json:"expected_version"`
	RootCause       string `json:"root_cause"`
	LessonsLearned  string `json:"lessons_learned"`
}

func (h *Handlers) transition(w http.ResponseWriter, r *http.Request) {
	id, ok := parseUUID(w, chi.URLParam(r, "incidentID"))
	if !ok {
		return
	}
	var req transitionRequest
	if !decode(w, r, &req) {
		return
	}
	if req.ExpectedVersion <= 0 {
		writeJSON(w, http.StatusBadRequest, errorBody{
			Error: stringErr(domain.ErrValidation), Message: "expected_version is required and positive",
		})
		return
	}
	res, err := h.svc.Transition(r.Context(), auth.User(r.Context()), id, service.TransitionInput{
		Action:          req.Action,
		ExpectedVersion: req.ExpectedVersion,
		RootCause:       req.RootCause,
		LessonsLearned:  req.LessonsLearned,
		RequestID:       requestID(r),
	})
	if err != nil {
		writeError(w, err)
		return
	}
	renderResult(w, res)
}

func (h *Handlers) exportIncident(w http.ResponseWriter, r *http.Request) {
	id, ok := parseUUID(w, chi.URLParam(r, "incidentID"))
	if !ok {
		return
	}
	report, err := h.svc.Export(r.Context(), auth.User(r.Context()), id)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, report)
}

func (h *Handlers) listMembers(w http.ResponseWriter, r *http.Request) {
	id, ok := parseUUID(w, chi.URLParam(r, "incidentID"))
	if !ok {
		return
	}
	members, err := h.svc.ListMembers(r.Context(), auth.User(r.Context()), id)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"members": members})
}

type assignMemberRequest struct {
	CaseRole string `json:"case_role"`
}

func (h *Handlers) assignMember(w http.ResponseWriter, r *http.Request) {
	incidentID, ok := parseUUID(w, chi.URLParam(r, "incidentID"))
	if !ok {
		return
	}
	userID, ok := parseUUID(w, chi.URLParam(r, "userID"))
	if !ok {
		return
	}
	var req assignMemberRequest
	if !decode(w, r, &req) {
		return
	}
	res, err := h.svc.AssignMember(r.Context(), auth.User(r.Context()), incidentID, userID, req.CaseRole)
	if err != nil {
		writeError(w, err)
		return
	}
	renderResult(w, res)
}

// --- helpers ---

func parseUUID(w http.ResponseWriter, raw string) (uuid.UUID, bool) {
	id, err := uuid.Parse(raw)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, errorBody{
			Error: "validation_error", Message: "invalid UUID: " + raw,
		})
		return uuid.Nil, false
	}
	return id, true
}

func stringErr(e error) string {
	// error codes for validation are emitted directly.
	return "validation_error"
}
