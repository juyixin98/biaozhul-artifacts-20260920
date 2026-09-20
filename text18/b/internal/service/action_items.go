package service

import (
	"errors"
	"net/http"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"sircc/internal/db"
	"sircc/internal/domain"
	"sircc/internal/httpx"
	"sircc/internal/middleware"
)

type createActionItemRequest struct {
	Title   string    `json:"title"`
	OwnerID string    `json:"owner_id"`
	DueAt   time.Time `json:"due_at"`
}

func (s *Service) CreateActionItem(w http.ResponseWriter, r *http.Request) {
	requestID := middleware.RequestID(r)
	incidentID := chi.URLParam(r, "incidentID")
	actor, _ := actorFrom(r.Context())

	var req createActionItemRequest
	if err := httpx.Decode(r, &req); err != nil {
		httpx.ErrorJSON(w, http.StatusBadRequest, httpx.CodeBadRequest, "invalid JSON body")
		return
	}
	if req.Title == "" {
		httpx.ErrorJSON(w, http.StatusBadRequest, httpx.CodeBadRequest, "title is required")
		return
	}
	if req.OwnerID == "" {
		httpx.ErrorJSON(w, http.StatusBadRequest, httpx.CodeBadRequest, "owner_id is required")
		return
	}
	if req.DueAt.IsZero() {
		httpx.ErrorJSON(w, http.StatusBadRequest, httpx.CodeBadRequest, "due_at is required (RFC3339)")
		return
	}

	s.idemExec(w, r, requestID, func(ctx ctxType, q db.Querier) result {
		if _, err := q.GetIncident(ctx, incidentID); err != nil {
			return errResult(http.StatusNotFound, httpx.CodeNotFound, "incident not found")
		}
		// Scope: members create items. Owner must be a known user.
		if actor.Role != domain.RoleAdmin {
			member, _ := q.IsIncidentMember(ctx, db.IsIncidentMemberParams{
				IncidentID: incidentID, UserID: actor.ID,
			})
			if !member {
				return errResult(http.StatusForbidden, httpx.CodeForbidden, "not a member of this incident")
			}
		}
		owner, err := q.GetUser(ctx, req.OwnerID)
		if err != nil {
			return errResult(http.StatusBadRequest, httpx.CodeBadRequest, "unknown owner")
		}
		ai, err := q.CreateActionItem(ctx, db.CreateActionItemParams{
			ID:         uuid.NewString(),
			IncidentID: incidentID,
			Title:      req.Title,
			OwnerID:    owner.ID,
			DueAt:      tstz(req.DueAt),
			CreatedBy:  actor.ID,
		})
		if err != nil {
			return errResult(http.StatusInternalServerError, httpx.CodeInternal, "create action item")
		}
		// The owner gains case access so they can see the work assigned.
		if err := q.AddIncidentMember(ctx, db.AddIncidentMemberParams{
			IncidentID: incidentID, UserID: owner.ID,
		}); err != nil {
			return errResult(http.StatusInternalServerError, httpx.CodeInternal, "add owner member")
		}
		if err := q.InsertAuditEvent(ctx, db.InsertAuditEventParams{
			ID: uuid.NewString(), IncidentID: textParam(incidentID), ActorID: actor.ID,
			Action: "action_item.created",
			Detail: mustJSON(map[string]any{
				"action_item_id": ai.ID, "owner_id": ai.OwnerID, "due_at": ai.DueAt,
			}),
		}); err != nil {
			return errResult(http.StatusInternalServerError, httpx.CodeInternal, "audit")
		}
		return okResult(ai)
	})
}

func (s *Service) ListActionItems(w http.ResponseWriter, r *http.Request) {
	incidentID := chi.URLParam(r, "incidentID")
	actor, _ := actorFrom(r.Context())
	if !s.canRead(w, r, actor, incidentID) {
		return
	}
	rows, err := s.q.ListActionItems(r.Context(), incidentID)
	if err != nil {
		httpx.ErrorJSON(w, http.StatusInternalServerError, httpx.CodeInternal, "list action items")
		return
	}
	httpx.JSON(w, http.StatusOK, map[string]any{"action_items": rows})
}

type rescheduleRequest struct {
	NewDueAt        time.Time `json:"new_due_at"`
	ExpectedVersion int32     `json:"expected_version"`
}

// RescheduleActionItem bumps due_at and version atomically. The optimistic
// version guard means a stale concurrent reschedule is rejected, so an old
// schedule can never overwrite a newer one and can never fire a reminder
// (reminders key on version).
func (s *Service) RescheduleActionItem(w http.ResponseWriter, r *http.Request) {
	requestID := middleware.RequestID(r)
	itemID := chi.URLParam(r, "itemID")
	actor, _ := actorFrom(r.Context())

	var req rescheduleRequest
	if err := httpx.Decode(r, &req); err != nil {
		httpx.ErrorJSON(w, http.StatusBadRequest, httpx.CodeBadRequest, "invalid JSON body")
		return
	}
	if req.NewDueAt.IsZero() {
		httpx.ErrorJSON(w, http.StatusBadRequest, httpx.CodeBadRequest, "new_due_at is required (RFC3339)")
		return
	}
	if req.ExpectedVersion <= 0 {
		httpx.ErrorJSON(w, http.StatusBadRequest, httpx.CodeBadRequest, "expected_version is required and positive")
		return
	}

	s.idemExec(w, r, requestID, func(ctx ctxType, q db.Querier) result {
		ai, err := q.GetActionItemForUpdate(ctx, itemID)
		if err != nil {
			return errResult(http.StatusNotFound, httpx.CodeNotFound, "action item not found")
		}
		// Scope.
		if actor.Role != domain.RoleAdmin {
			member, _ := q.IsIncidentMember(ctx, db.IsIncidentMemberParams{
				IncidentID: ai.IncidentID, UserID: actor.ID,
			})
			if !member {
				return errResult(http.StatusForbidden, httpx.CodeForbidden, "not a member of this incident")
			}
		}
		if ai.Version != req.ExpectedVersion {
			return errResult(http.StatusConflict, httpx.CodeVersionConflict,
				"expected_version does not match current action item version")
		}
		oldDue := ai.DueAt
		fromVersion := ai.Version

		updated, err := q.RescheduleActionItem(ctx, db.RescheduleActionItemParams{
			ID:      itemID,
			DueAt:   tstz(req.NewDueAt),
			Version: req.ExpectedVersion,
		})
		if err != nil {
			if errors.Is(err, pgx.ErrNoRows) {
				return errResult(http.StatusConflict, httpx.CodeVersionConflict, "reschedule lost race")
			}
			return errResult(http.StatusInternalServerError, httpx.CodeInternal, "reschedule")
		}
		if err := q.InsertRescheduleAudit(ctx, db.InsertRescheduleAuditParams{
			ID:           uuid.NewString(),
			ActionItemID: itemID,
			FromVersion:  fromVersion,
			ToVersion:    updated.Version,
			OldDueAt:     oldDue,
			NewDueAt:     updated.DueAt,
			ActorID:      actor.ID,
			RequestID:    requestID,
		}); err != nil {
			return errResult(http.StatusInternalServerError, httpx.CodeInternal, "reschedule audit")
		}
		if err := q.InsertAuditEvent(ctx, db.InsertAuditEventParams{
			ID: uuid.NewString(), IncidentID: textParam(ai.IncidentID), ActorID: actor.ID,
			Action: "action_item.rescheduled",
			Detail: mustJSON(map[string]any{
				"action_item_id": itemID,
				"from_version":   fromVersion,
				"to_version":     updated.Version,
			}),
		}); err != nil {
			return errResult(http.StatusInternalServerError, httpx.CodeInternal, "audit")
		}
		return okResult(updated)
	})
}

type updateStatusRequest struct {
	Status string `json:"status"`
}

func (s *Service) UpdateActionItemStatus(w http.ResponseWriter, r *http.Request) {
	requestID := middleware.RequestID(r)
	itemID := chi.URLParam(r, "itemID")
	actor, _ := actorFrom(r.Context())

	var req updateStatusRequest
	if err := httpx.Decode(r, &req); err != nil {
		httpx.ErrorJSON(w, http.StatusBadRequest, httpx.CodeBadRequest, "invalid JSON body")
		return
	}
	if req.Status != "open" && req.Status != "done" && req.Status != "cancelled" {
		httpx.ErrorJSON(w, http.StatusBadRequest, httpx.CodeBadRequest, "status must be open, done or cancelled")
		return
	}

	s.idemExec(w, r, requestID, func(ctx ctxType, q db.Querier) result {
		ai, err := q.GetActionItem(ctx, itemID)
		if err != nil {
			return errResult(http.StatusNotFound, httpx.CodeNotFound, "action item not found")
		}
		if actor.Role != domain.RoleAdmin {
			member, _ := q.IsIncidentMember(ctx, db.IsIncidentMemberParams{
				IncidentID: ai.IncidentID, UserID: actor.ID,
			})
			if !member {
				return errResult(http.StatusForbidden, httpx.CodeForbidden, "not a member of this incident")
			}
		}
		updated, err := q.UpdateActionItemStatus(ctx, db.UpdateActionItemStatusParams{
			ID: itemID, Status: req.Status,
		})
		if err != nil {
			return errResult(http.StatusInternalServerError, httpx.CodeInternal, "update status")
		}
		if err := q.InsertAuditEvent(ctx, db.InsertAuditEventParams{
			ID: uuid.NewString(), IncidentID: textParam(ai.IncidentID), ActorID: actor.ID,
			Action: "action_item.status_changed",
			Detail: mustJSON(map[string]any{"action_item_id": itemID, "status": req.Status}),
		}); err != nil {
			return errResult(http.StatusInternalServerError, httpx.CodeInternal, "audit")
		}
		return okResult(updated)
	})
}

func (s *Service) ListNotifications(w http.ResponseWriter, r *http.Request) {
	actor, _ := actorFrom(r.Context())
	q := r.URL.Query()
	limit, offset := parsePagination(q.Get("limit"), q.Get("offset"))
	// Admins may request all notifications; everyone else sees only their own.
	includeAll := actor.Role == domain.RoleAdmin && q.Get("scope") == "all"
	rows, err := s.q.ListNotifications(r.Context(), db.ListNotificationsParams{
		IncludeAll: includeAll,
		OwnerID:    actor.ID,
		Limit:      limit,
		Offset:     offset,
	})
	if err != nil {
		httpx.ErrorJSON(w, http.StatusInternalServerError, httpx.CodeInternal, "list notifications")
		return
	}
	httpx.JSON(w, http.StatusOK, map[string]any{"notifications": rows})
}
