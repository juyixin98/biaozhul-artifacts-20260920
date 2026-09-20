package service

import (
	"net/http"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"

	"sircc/internal/db"
	"sircc/internal/domain"
	"sircc/internal/httpx"
	"sircc/internal/middleware"
)

type createIncidentRequest struct {
	Title    string `json:"title"`
	Severity string `json:"severity"`
}

func (s *Service) CreateIncident(w http.ResponseWriter, r *http.Request) {
	requestID := middleware.RequestID(r)
	var req createIncidentRequest
	if err := httpx.Decode(r, &req); err != nil {
		httpx.ErrorJSON(w, http.StatusBadRequest, httpx.CodeBadRequest, "invalid JSON body")
		return
	}
	if req.Title == "" {
		httpx.ErrorJSON(w, http.StatusBadRequest, httpx.CodeBadRequest, "title is required")
		return
	}
	if req.Severity != "P1" && req.Severity != "P2" && req.Severity != "P3" && req.Severity != "P4" {
		httpx.ErrorJSON(w, http.StatusBadRequest, httpx.CodeBadRequest, "severity must be one of P1..P4")
		return
	}
	actor, _ := actorFrom(r.Context())

	s.idemExec(w, r, requestID, func(ctx ctxType, q db.Querier) result {
		id := uuid.NewString()
		inc, err := q.CreateIncident(ctx, db.CreateIncidentParams{
			ID:        id,
			Title:     req.Title,
			Severity:  req.Severity,
			CreatedBy: actor.ID,
		})
		if err != nil {
			return errResult(http.StatusInternalServerError, httpx.CodeInternal, "create incident")
		}
		// Creator is always a member and sees their case.
		if err := q.AddIncidentMember(ctx, db.AddIncidentMemberParams{
			IncidentID: inc.ID, UserID: actor.ID,
		}); err != nil {
			return errResult(http.StatusInternalServerError, httpx.CodeInternal, "add creator member")
		}
		if err := q.InsertAuditEvent(ctx, db.InsertAuditEventParams{
			ID: uuid.NewString(), IncidentID: textParam(inc.ID), ActorID: actor.ID,
			Action: "incident.created",
			Detail: mustJSON(map[string]string{"severity": req.Severity}),
		}); err != nil {
			return errResult(http.StatusInternalServerError, httpx.CodeInternal, "audit")
		}
		return okResult(toIncidentResponse(inc))
	})
}

func (s *Service) GetIncident(w http.ResponseWriter, r *http.Request) {
	id := chi.URLParam(r, "incidentID")
	actor, _ := actorFrom(r.Context())

	inc, err := s.q.GetIncident(r.Context(), id)
	if err != nil {
		httpx.ErrorJSON(w, http.StatusNotFound, httpx.CodeNotFound, "incident not found")
		return
	}
	if actor.Role != domain.RoleAdmin {
		member, _ := s.q.IsIncidentMember(r.Context(), db.IsIncidentMemberParams{
			IncidentID: id, UserID: actor.ID,
		})
		if !member {
			httpx.ErrorJSON(w, http.StatusForbidden, httpx.CodeForbidden, "not a member of this incident")
			return
		}
	}
	httpx.JSON(w, http.StatusOK, toIncidentResponse(inc))
}

func (s *Service) ListIncidents(w http.ResponseWriter, r *http.Request) {
	actor, _ := actorFrom(r.Context())
	q := r.URL.Query()
	limit, offset := parsePagination(q.Get("limit"), q.Get("offset"))

	// Non-admins only see cases they belong to.
	assigneeFilter := actor.Role != domain.RoleAdmin
	if scope := q.Get("scope"); scope == "all" && actor.Role == domain.RoleAdmin {
		assigneeFilter = false
	}
	if scope := q.Get("scope"); scope == "mine" {
		assigneeFilter = true
	}

	rows, err := s.q.ListIncidents(r.Context(), db.ListIncidentsParams{
		AssigneeFilter: assigneeFilter,
		UserID:         textParam(actor.ID),
		Severity:       q.Get("severity"),
		Limit:          limit,
		Offset:         offset,
	})
	if err != nil {
		httpx.ErrorJSON(w, http.StatusInternalServerError, httpx.CodeInternal, "list incidents")
		return
	}
	out := make([]incidentResponse, 0, len(rows))
	for _, inc := range rows {
		out = append(out, toIncidentResponse(inc))
	}
	httpx.JSON(w, http.StatusOK, map[string]any{"incidents": out})
}

type assignRequest struct {
	UserID string `json:"user_id"`
	Role   string `json:"role"`
}

func (s *Service) AssignPerson(w http.ResponseWriter, r *http.Request) {
	requestID := middleware.RequestID(r)
	incidentID := chi.URLParam(r, "incidentID")
	actor, _ := actorFrom(r.Context())

	var req assignRequest
	if err := httpx.Decode(r, &req); err != nil {
		httpx.ErrorJSON(w, http.StatusBadRequest, httpx.CodeBadRequest, "invalid JSON body")
		return
	}
	if req.Role != domain.RoleAnalyst && req.Role != domain.RoleResponder {
		httpx.ErrorJSON(w, http.StatusBadRequest, httpx.CodeBadRequest, "role must be analyst or responder")
		return
	}
	if !domain.CanAssign(actor.Role) {
		httpx.ErrorJSON(w, http.StatusForbidden, httpx.CodeForbidden, "only admins assign people")
		return
	}

	s.idemExec(w, r, requestID, func(ctx ctxType, q db.Querier) result {
		inc, err := q.GetIncidentForUpdate(ctx, incidentID)
		if err != nil {
			return errResult(http.StatusNotFound, httpx.CodeNotFound, "incident not found")
		}
		assignee, err := q.GetUser(ctx, req.UserID)
		if err != nil {
			return errResult(http.StatusBadRequest, httpx.CodeBadRequest, "unknown assignee user")
		}
		if assignee.Role != req.Role {
			return errResult(http.StatusBadRequest, httpx.CodeBadRequest,
				"user role does not match requested assignment role")
		}
		if err := q.AssignIncidentPerson(ctx, db.AssignIncidentPersonParams{
			ID: inc.ID, Role: req.Role, UserID: textParam(req.UserID),
		}); err != nil {
			return errResult(http.StatusInternalServerError, httpx.CodeInternal, "assign")
		}
		if err := q.AddIncidentMember(ctx, db.AddIncidentMemberParams{
			IncidentID: inc.ID, UserID: req.UserID,
		}); err != nil {
			return errResult(http.StatusInternalServerError, httpx.CodeInternal, "add member")
		}
		if err := q.InsertAuditEvent(ctx, db.InsertAuditEventParams{
			ID: uuid.NewString(), IncidentID: textParam(inc.ID), ActorID: actor.ID,
			Action: "person.assigned",
			Detail: mustJSON(map[string]string{"user_id": req.UserID, "role": req.Role}),
		}); err != nil {
			return errResult(http.StatusInternalServerError, httpx.CodeInternal, "audit")
		}
		updated, err := q.GetIncident(ctx, inc.ID)
		if err != nil {
			return errResult(http.StatusInternalServerError, httpx.CodeInternal, "reload")
		}
		return okResult(toIncidentResponse(updated))
	})
}

type transitionRequest struct {
	ExpectedVersion int64  `json:"expected_version"`
	Note            string `json:"note"`
	// Only honored when entering postmortem / closing.
	RootCause      string `json:"root_cause"`
	LessonsLearned string `json:"lessons_learned"`
}

// Transition moves the incident to its next stage. All guarantees live here:
// role permission, case scope, exact forward edge, optimistic version, P1
// triage gate, close gate, and the atomic stage+time+audit write.
func (s *Service) Transition(w http.ResponseWriter, r *http.Request) {
	requestID := middleware.RequestID(r)
	incidentID := chi.URLParam(r, "incidentID")
	actor, _ := actorFrom(r.Context())

	var req transitionRequest
	if err := httpx.Decode(r, &req); err != nil {
		httpx.ErrorJSON(w, http.StatusBadRequest, httpx.CodeBadRequest, "invalid JSON body")
		return
	}
	if req.ExpectedVersion <= 0 {
		httpx.ErrorJSON(w, http.StatusBadRequest, httpx.CodeBadRequest, "expected_version is required and positive")
		return
	}

	s.idemExec(w, r, requestID, func(ctx ctxType, q db.Querier) result {
		// Lock the row first so the permission/scope checks and the
		// conditional update operate on one stable snapshot.
		inc, err := q.GetIncidentForUpdate(ctx, incidentID)
		if err != nil {
			return errResult(http.StatusNotFound, httpx.CodeNotFound, "incident not found")
		}

		// Case scope.
		if actor.Role != domain.RoleAdmin {
			member, _ := q.IsIncidentMember(ctx, db.IsIncidentMemberParams{
				IncidentID: inc.ID, UserID: actor.ID,
			})
			if !member {
				return errResult(http.StatusForbidden, httpx.CodeForbidden, "not a member of this incident")
			}
		}

		// Stale optimistic-lock version. Checked before role so a racing
		// loser is told the truth (409), not mislabeled as forbidden.
		if inc.Version != req.ExpectedVersion {
			return errResult(http.StatusConflict, httpx.CodeVersionConflict,
				"expected_version does not match current version")
		}

		to := domain.NextStage(inc.Stage)
		if to == "" {
			return errResult(http.StatusConflict, httpx.CodeStageConflict, "incident is already closed")
		}
		if !domain.CanTransition(actor.Role, inc.Stage, to) {
			return errResult(http.StatusForbidden, httpx.CodeForbidden,
				"role not permitted to perform this transition")
		}

		// P1 triage gate: a responder must be assigned before triage can
		// complete.
		if inc.Stage == domain.StageDetected && to == domain.StageTriaged &&
			inc.Severity == "P1" && !inc.AssignedResponderID.Valid {
			return errResult(http.StatusUnprocessableEntity, httpx.CodeGateFailed,
				"P1 incidents require an assigned responder before triage")
		}

		// Postmortem payload validation (clear error before the SQL gate).
		// Values may be supplied now or already be persisted on the incident;
		// a supplied non-empty value wins and is carried forward.
		var rootCause, lessons pgtype.Text
		if to == domain.StagePostmortem || to == domain.StageClosed {
			rootCause = mergeText(req.RootCause, inc.RootCause)
			lessons = mergeText(req.LessonsLearned, inc.LessonsLearned)
			if !rootCause.Valid {
				return errResult(http.StatusUnprocessableEntity, httpx.CodeGateFailed,
					"root_cause is required before closing")
			}
			if !lessons.Valid {
				return errResult(http.StatusUnprocessableEntity, httpx.CodeGateFailed,
					"lessons_learned is required before closing")
			}
		}
		if to == domain.StageClosed {
			open, _ := q.CountOpenActionItems(ctx, inc.ID)
			if open > 0 {
				return errResult(http.StatusUnprocessableEntity, httpx.CodeGateFailed,
					"all action items must be resolved before closing")
			}
			anyItems, _ := q.CountActionItemsAny(ctx, inc.ID)
			if !anyItems {
				return errResult(http.StatusUnprocessableEntity, httpx.CodeGateFailed,
					"at least one action item with owner and due date is required before closing")
			}
		}

		updated, err := q.TransitionIncident(ctx, db.TransitionIncidentParams{
			IncidentID:      inc.ID,
			ExpectedVersion: req.ExpectedVersion,
			FromStage:       inc.Stage,
			ToStage:         to,
			RootCause:       rootCause,
			LessonsLearned:  lessons,
		})
		if err != nil {
			if err == pgx.ErrNoRows {
				// Changed between lock and update should be impossible, but
				// report honestly rather than fake success.
				return errResult(http.StatusConflict, httpx.CodeVersionConflict, "transition lost race")
			}
			return errResult(http.StatusInternalServerError, httpx.CodeInternal, "transition")
		}

		if _, err := q.InsertStageEvent(ctx, db.InsertStageEventParams{
			ID:         uuid.NewString(),
			IncidentID: inc.ID,
			Version:    updated.Version,
			FromStage:  inc.Stage,
			ToStage:    to,
			ActorID:    actor.ID,
			Note:       req.Note,
			RequestID:  requestID,
		}); err != nil {
			return errResult(http.StatusInternalServerError, httpx.CodeInternal, "stage event")
		}
		if err := q.InsertAuditEvent(ctx, db.InsertAuditEventParams{
			ID: uuid.NewString(), IncidentID: textParam(inc.ID), ActorID: actor.ID,
			Action: "stage.transition",
			Detail: mustJSON(map[string]any{
				"from": inc.Stage, "to": to, "version": updated.Version,
			}),
		}); err != nil {
			return errResult(http.StatusInternalServerError, httpx.CodeInternal, "audit")
		}
		return okResult(toIncidentResponse(updated))
	})
}

// ListStageEvents returns the stage timeline for a case (scope checked).
func (s *Service) ListStageEvents(w http.ResponseWriter, r *http.Request) {
	id := chi.URLParam(r, "incidentID")
	actor, _ := actorFrom(r.Context())
	if !s.canRead(w, r, actor, id) {
		return
	}
	rows, err := s.q.ListStageEvents(r.Context(), id)
	if err != nil {
		httpx.ErrorJSON(w, http.StatusInternalServerError, httpx.CodeInternal, "stage events")
		return
	}
	httpx.JSON(w, http.StatusOK, map[string]any{"events": rows})
}

// mergeText prefers a non-empty supplied value and falls back to a value
// already persisted on the row. It returns an invalid pgtype.Text only when
// neither source has content.
func mergeText(supplied string, existing pgtype.Text) pgtype.Text {
	if supplied != "" {
		return pgtype.Text{String: supplied, Valid: true}
	}
	if existing.Valid && existing.String != "" {
		return existing
	}
	return pgtype.Text{}
}

// incidentResponse is the wire shape; timestamps serialize as RFC3339 and
// unset stage times are omitted.
type incidentResponse struct {
	ID                  string     `json:"id"`
	Title               string     `json:"title"`
	Severity            string     `json:"severity"`
	Stage               string     `json:"stage"`
	Version             int64      `json:"version"`
	CreatedBy           string     `json:"created_by"`
	AssignedAnalystID   *string    `json:"assigned_analyst_id,omitempty"`
	AssignedResponderID *string    `json:"assigned_responder_id,omitempty"`
	RootCause           *string    `json:"root_cause,omitempty"`
	LessonsLearned      *string    `json:"lessons_learned,omitempty"`
	DetectedAt          time.Time  `json:"detected_at"`
	TriagedAt           *time.Time `json:"triaged_at,omitempty"`
	ContainedAt         *time.Time `json:"contained_at,omitempty"`
	EradicatedAt        *time.Time `json:"eradicated_at,omitempty"`
	RecoveredAt         *time.Time `json:"recovered_at,omitempty"`
	PostmortemAt        *time.Time `json:"postmortem_at,omitempty"`
	ClosedAt            *time.Time `json:"closed_at,omitempty"`
	CreatedAt           time.Time  `json:"created_at"`
}

func toIncidentResponse(inc db.Incident) incidentResponse {
	return incidentResponse{
		ID:                  inc.ID,
		Title:               inc.Title,
		Severity:            inc.Severity,
		Stage:               inc.Stage,
		Version:             inc.Version,
		CreatedBy:           inc.CreatedBy,
		AssignedAnalystID:   pstr(inc.AssignedAnalystID),
		AssignedResponderID: pstr(inc.AssignedResponderID),
		RootCause:           pstr(inc.RootCause),
		LessonsLearned:      pstr(inc.LessonsLearned),
		DetectedAt:          inc.DetectedAt.Time,
		TriagedAt:           ts(inc.TriagedAt),
		ContainedAt:         ts(inc.ContainedAt),
		EradicatedAt:        ts(inc.EradicatedAt),
		RecoveredAt:         ts(inc.RecoveredAt),
		PostmortemAt:        ts(inc.PostmortemAt),
		ClosedAt:            ts(inc.ClosedAt),
		CreatedAt:           inc.CreatedAt.Time,
	}
}
