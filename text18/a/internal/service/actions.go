package service

import (
	"context"
	"log"
	"net/http"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"sircc/internal/domain"
	"sircc/internal/store"
)

type CreateActionItemInput struct {
	Description string
	OwnerUserID uuid.UUID
	DueAt       time.Time
	RequestID   uuid.UUID
}

// CreateActionItem attaches an owned, deadline-bearing action item to a
// case. Any case member (or admin) may create one; the owner must be an
// active user.
func (s *Service) CreateActionItem(ctx context.Context, caller *store.User,
	incidentID uuid.UUID, in CreateActionItemInput,
) (Result, error) {
	in.Description = strings.TrimSpace(in.Description)
	if in.Description == "" || len(in.Description) > 10000 {
		return Result{}, domain.ErrValidation
	}
	if in.DueAt.IsZero() || !in.DueAt.After(time.Date(2000, 1, 1, 0, 0, 0, 0, time.UTC)) {
		return Result{}, domain.ErrValidation
	}
	incident := incidentID

	return s.runMutated(ctx, caller.ID, in.RequestID, &incident, "POST",
		"/api/v1/incidents/"+incidentID.String()+"/action-items",
		func(ctx context.Context, q *store.Queries) (any, int, error) {
			inc, err := q.GetIncidentForUpdate(ctx, incidentID)
			if err != nil {
				if err == pgx.ErrNoRows {
					return nil, 0, domain.ErrNotFound
				}
				return nil, 0, err
			}
			if inc.Status == domain.StatusClosed {
				return nil, 0, domain.ErrInvalidTransition
			}
			if caller.Role != domain.RoleAdmin {
				if _, err := s.requireCaseAccess(ctx, q, incidentID, caller,
					domain.RoleAnalyst, domain.RoleResponder); err != nil {
					return nil, 0, err
				}
			}
			owner, err := q.UserByID(ctx, in.OwnerUserID)
			if err != nil {
				if err == pgx.ErrNoRows {
					return nil, 0, domain.ErrValidation
				}
				return nil, 0, err
			}
			item, err := q.CreateActionItem(ctx, store.CreateActionItemParams{
				IncidentID:  incidentID,
				Description: in.Description,
				OwnerUserID: owner.ID,
				DueAt:       pgTimestamp(in.DueAt.UTC()),
				CreatedBy:   caller.ID,
			})
			if err != nil {
				return nil, 0, err
			}
			if _, err := q.CreateAuditEvent(ctx, store.CreateAuditEventParams{
				IncidentID: incidentID,
				ActorID:    caller.ID,
				Action:     "action_item.created",
				FromStatus: strPtr(inc.Status),
				ToStatus:   strPtr(inc.Status),
				RequestID:  pgUUID(in.RequestID),
				Detail: []byte(`{"action_item_id":"` + item.ID.String() +
					`","due_version":1,"due_at":"` + item.DueAt.Time.UTC().Format(time.RFC3339) + `"}`),
			}); err != nil {
				return nil, 0, err
			}
			return actionItemView(item, owner.FullName, owner.Username), http.StatusCreated, nil
		})
}

type RescheduleInput struct {
	DueAt     time.Time
	RequestID uuid.UUID
}

// RescheduleActionItem moves the deadline. The schedule version is bumped
// atomically; only the currently stored (due_at, due_version) can ever be
// reminded, so an old schedule can never emit a stale reminder.
func (s *Service) RescheduleActionItem(ctx context.Context, caller *store.User,
	incidentID, itemID uuid.UUID, in RescheduleInput,
) (Result, error) {
	if in.DueAt.IsZero() {
		return Result{}, domain.ErrValidation
	}
	incident := incidentID

	return s.runMutated(ctx, caller.ID, in.RequestID, &incident, "POST",
		"/api/v1/incidents/"+incidentID.String()+"/action-items/"+itemID.String()+"/reschedule",
		func(ctx context.Context, q *store.Queries) (any, int, error) {
			if _, err := q.GetIncidentForUpdate(ctx, incidentID); err != nil {
				if err == pgx.ErrNoRows {
					return nil, 0, domain.ErrNotFound
				}
				return nil, 0, err
			}
			if caller.Role != domain.RoleAdmin {
				if _, err := s.requireCaseAccess(ctx, q, incidentID, caller,
					domain.RoleAnalyst, domain.RoleResponder); err != nil {
					return nil, 0, err
				}
			}
			existing, err := q.GetActionItemForUpdate(ctx, store.GetActionItemForUpdateParams{
				ID:         itemID,
				IncidentID: incidentID,
			})
			if err != nil {
				if err == pgx.ErrNoRows {
					return nil, 0, domain.ErrNotFound
				}
				return nil, 0, err
			}

			item, err := q.RescheduleActionItem(ctx, store.RescheduleActionItemParams{
				ID:         itemID,
				IncidentID: incidentID,
				DueAt:      pgTimestamp(in.DueAt.UTC()),
			})
			if err != nil {
				if err == pgx.ErrNoRows {
					return nil, 0, domain.ErrNotFound
				}
				return nil, 0, err
			}
			if _, err := q.CreateAuditEvent(ctx, store.CreateAuditEventParams{
				IncidentID: incidentID,
				ActorID:    caller.ID,
				Action:     "action_item.rescheduled",
				RequestID:  pgUUID(in.RequestID),
				Detail: []byte(`{"action_item_id":"` + item.ID.String() +
					`","old_due_version":` + itoa(int64(existing.DueVersion)) +
					`,"new_due_version":` + itoa(int64(item.DueVersion)) +
					`,"old_due_at":"` + existing.DueAt.Time.UTC().Format(time.RFC3339) +
					`","new_due_at":"` + item.DueAt.Time.UTC().Format(time.RFC3339) + `"}`),
			}); err != nil {
				return nil, 0, err
			}
			return actionItemView(item, "", ""), http.StatusOK, nil
		})
}

// SetActionItemStatus marks an item done or canceled.
func (s *Service) SetActionItemStatus(ctx context.Context, caller *store.User,
	incidentID, itemID uuid.UUID, status string,
) (Result, error) {
	if status != "done" && status != "canceled" {
		return Result{}, domain.ErrValidation
	}
	incident := incidentID
	return s.runMutated(ctx, caller.ID, uuid.Nil, &incident, "POST",
		"/api/v1/incidents/"+incidentID.String()+"/action-items/"+itemID.String()+"/status",
		func(ctx context.Context, q *store.Queries) (any, int, error) {
			if _, err := q.GetIncidentForUpdate(ctx, incidentID); err != nil {
				if err == pgx.ErrNoRows {
					return nil, 0, domain.ErrNotFound
				}
				return nil, 0, err
			}
			if caller.Role != domain.RoleAdmin {
				if _, err := s.requireCaseAccess(ctx, q, incidentID, caller,
					domain.RoleAnalyst, domain.RoleResponder); err != nil {
					return nil, 0, err
				}
			}
			item, err := q.SetActionItemStatus(ctx, store.SetActionItemStatusParams{
				ID:         itemID,
				IncidentID: incidentID,
				Status:     status,
			})
			if err != nil {
				if err == pgx.ErrNoRows {
					return nil, 0, domain.ErrNotFound
				}
				return nil, 0, err
			}
			if _, err := q.CreateAuditEvent(ctx, store.CreateAuditEventParams{
				IncidentID: incidentID,
				ActorID:    caller.ID,
				Action:     "action_item." + status,
				Detail:     []byte(`{"action_item_id":"` + itemID.String() + `","due_version":` + itoa(int64(item.DueVersion)) + `}`),
			}); err != nil {
				return nil, 0, err
			}
			return actionItemView(item, "", ""), http.StatusOK, nil
		})
}

// ListActionItems returns the case's action items and any reminders they
// produced. Caller must have case scope.
func (s *Service) ListActionItems(ctx context.Context, caller *store.User,
	incidentID uuid.UUID,
) ([]ActionItemView, []ReminderView, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, nil, err
	}
	defer tx.Rollback(ctx)
	q := store.New(tx)
	if _, err := q.GetIncident(ctx, incidentID); err != nil {
		if err == pgx.ErrNoRows {
			return nil, nil, domain.ErrNotFound
		}
		return nil, nil, err
	}
	if _, err := s.requireCaseAccess(ctx, q, incidentID, caller); err != nil {
		return nil, nil, err
	}
	items, err := q.ListActionItemsByIncident(ctx, incidentID)
	if err != nil {
		return nil, nil, err
	}
	out := make([]ActionItemView, 0, len(items))
	for _, it := range items {
		out = append(out, actionItemRowView(it))
	}
	reminders, err := q.ListRemindersByIncident(ctx, incidentID)
	if err != nil {
		return nil, nil, err
	}
	rv := make([]ReminderView, 0, len(reminders))
	for _, r := range reminders {
		rv = append(rv, reminderView(r))
	}
	return out, rv, tx.Commit(ctx)
}

// SweepDueOnce claims every open action item whose deadline has passed and
// persists exactly one reminder per (item, due_version). It is safe to run
// concurrently (FOR UPDATE SKIP LOCKED plus a unique constraint) and on
// startup (missed deadlines are simply rows that are already due).
func (s *Service) SweepDueOnce(ctx context.Context, batchSize int32) (int, error) {
	now := s.clock.Now()
	reminded := 0
	err := s.inTx(ctx, func(q *store.Queries) error {
		items, err := q.DueActionItems(ctx, store.DueActionItemsParams{
			DueAt: pgTimestamp(now),
			Limit: batchSize,
		})
		if err != nil {
			return err
		}
		for _, item := range items {
			msg := "action item overdue: " + item.Description +
				" (due " + item.DueAt.Time.UTC().Format(time.RFC3339) + ")"
			created, err := q.CreateReminder(ctx, store.CreateReminderParams{
				ActionItemID: item.ID,
				IncidentID:   item.IncidentID,
				DueVersion:   item.DueVersion,
				DueAt:        item.DueAt,
				Message:      msg,
			})
			if err != nil {
				if err == pgx.ErrNoRows {
					// Another sweep already reminded this exact
					// (item, due_version): the unique constraint made the
					// insert a no-op. Do not remind twice.
					continue
				}
				return err
			}
			reminded++
			if _, err := q.CreateAuditEvent(ctx, store.CreateAuditEventParams{
				IncidentID: item.IncidentID,
				ActorID:    item.CreatedBy,
				Action:     "action_item.due_reminded",
				Detail: []byte(`{"action_item_id":"` + item.ID.String() +
					`","due_version":` + itoa(int64(item.DueVersion)) +
					`,"reminder_id":"` + created.ID.String() + `"}`),
			}); err != nil {
				return err
			}
		}
		return nil
	})
	if err != nil {
		return 0, err
	}
	if reminded > 0 {
		log.Printf("scheduler: persisted %d overdue reminder(s) at %s", reminded, now.Format(time.RFC3339))
	}
	return reminded, nil
}

func actionItemView(a store.ActionItem, ownerFullName, ownerUsername string) ActionItemView {
	return ActionItemView{
		ID:            a.ID.String(),
		IncidentID:    a.IncidentID.String(),
		Description:   a.Description,
		OwnerUserID:   a.OwnerUserID.String(),
		OwnerUsername: ownerUsername,
		OwnerFullName: ownerFullName,
		DueAt:         ts(a.DueAt),
		DueVersion:    a.DueVersion,
		Status:        a.Status,
		CreatedBy:     a.CreatedBy.String(),
		CreatedAt:     ts(a.CreatedAt),
		UpdatedAt:     ts(a.UpdatedAt),
	}
}

func actionItemFromListRow(r store.ListActionItemsByIncidentRow) store.ActionItem {
	return store.ActionItem{
		ID:          r.ID,
		IncidentID:  r.IncidentID,
		Description: r.Description,
		OwnerUserID: r.OwnerUserID,
		DueAt:       r.DueAt,
		DueVersion:  r.DueVersion,
		Status:      r.Status,
		CreatedBy:   r.CreatedBy,
		CreatedAt:   r.CreatedAt,
		UpdatedAt:   r.UpdatedAt,
	}
}

func actionItemRowView(r store.ListActionItemsByIncidentRow) ActionItemView {
	return actionItemView(actionItemFromListRow(r), r.OwnerFullName, r.OwnerUsername)
}

func reminderView(r store.Reminder) ReminderView {
	return ReminderView{
		ID:           r.ID.String(),
		ActionItemID: r.ActionItemID.String(),
		IncidentID:   r.IncidentID.String(),
		DueVersion:   r.DueVersion,
		DueAt:        ts(r.DueAt),
		Message:      r.Message,
		CreatedAt:    ts(r.CreatedAt),
	}
}
