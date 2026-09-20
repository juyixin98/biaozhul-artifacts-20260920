package service

import (
	"net/http"

	"sircc/internal/domain"
	"sircc/internal/httpx"
)

// ListAuditEvents exposes the audit trail. Non-admins may read only the
// incidents they belong to; admins can use ?incident_id= or list globally.
func (s *Service) ListAuditEvents(w http.ResponseWriter, r *http.Request) {
	actor, _ := actorFrom(r.Context())
	q := r.URL.Query()
	limit, offset := parsePagination(q.Get("limit"), q.Get("offset"))

	incidentFilter := false
	incidentID := q.Get("incident_id")
	if incidentID != "" {
		incidentFilter = true
		// Scope: the incident must exist and the caller must be a member.
		if actor.Role != domain.RoleAdmin {
			if !s.canRead(w, r, actor, incidentID) {
				return
			}
		}
	} else if actor.Role != domain.RoleAdmin {
		// No global audit view for non-admins.
		httpx.ErrorJSON(w, http.StatusForbidden, httpx.CodeForbidden, "specify incident_id for a case you can access")
		return
	}

	rows, err := s.q.ListAuditEvents(r.Context(), queryAuditParams(incidentFilter, incidentID, limit, offset))
	if err != nil {
		httpx.ErrorJSON(w, http.StatusInternalServerError, httpx.CodeInternal, "list audit events")
		return
	}
	httpx.JSON(w, http.StatusOK, map[string]any{"audit_events": rows})
}
