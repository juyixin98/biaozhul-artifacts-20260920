package httpapi

import (
	"net/http"
	"time"

	"github.com/google/uuid"

	"sircc/internal/store"
)

type createActionItemRequest struct {
	Title   string    `json:"title"`
	OwnerID uuid.UUID `json:"ownerId"`
	DueAt   time.Time `json:"dueAt"`
}

func (s *Server) handleCreateActionItem(w http.ResponseWriter, r *http.Request) {
	u := actor(r)
	id, ae := urlUUID(r, "incidentID")
	if ae != nil {
		writeErr(w, ae)
		return
	}
	var req createActionItemRequest
	if ae := decodeJSON(r, &req); ae != nil {
		writeErr(w, ae)
		return
	}
	if req.Title == "" || req.OwnerID == uuid.Nil || req.DueAt.IsZero() {
		writeErr(w, errOf(http.StatusBadRequest, "validation",
			"title, ownerId and dueAt are required"))
		return
	}
	if _, err := s.q.GetUser(r.Context(), pgUUID(req.OwnerID)); isNotFound(err) {
		writeErr(w, errOf(http.StatusBadRequest, "validation", "owner user not found"))
		return
	} else if err != nil {
		writeErr(w, err)
		return
	}

	tx, err := s.pool.Begin(r.Context())
	if err != nil {
		writeErr(w, err)
		return
	}
	defer tx.Rollback(r.Context())
	q := s.q.WithTx(tx)

	inc, err := q.GetIncidentForUpdate(r.Context(), pgUUID(id))
	if isNotFound(err) {
		writeErr(w, errNotFound)
		return
	}
	if err != nil {
		writeErr(w, err)
		return
	}
	if ae := requireAssignee(u, inc); ae != nil {
		writeErr(w, ae)
		return
	}
	item, err := q.CreateActionItem(r.Context(), store.CreateActionItemParams{
		ID:         pgUUID(uuid.New()),
		IncidentID: pgUUID(id),
		Title:      req.Title,
		OwnerID:    pgUUID(req.OwnerID),
		DueAt:      pgtypeTimestamptz(req.DueAt),
	})
	if err != nil {
		writeErr(w, err)
		return
	}
	if err := insertAudit(r, q, pgUUID(id), &u.ID, "action_item.created", map[string]any{
		"actionItemId": fromPGUUID(item.ID).String(),
		"ownerId":      req.OwnerID.String(),
		"dueAt":        req.DueAt.UTC().Format(time.RFC3339),
	}); err != nil {
		writeErr(w, err)
		return
	}
	if err := tx.Commit(r.Context()); err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, toActionItemDTO(item))
}

func (s *Server) handleListActionItems(w http.ResponseWriter, r *http.Request) {
	id, ae := urlUUID(r, "incidentID")
	if ae != nil {
		writeErr(w, ae)
		return
	}
	if _, err := s.q.GetIncident(r.Context(), pgUUID(id)); isNotFound(err) {
		writeErr(w, errNotFound)
		return
	} else if err != nil {
		writeErr(w, err)
		return
	}
	rows, err := s.q.ListActionItemsByIncident(r.Context(), pgUUID(id))
	if err != nil {
		writeErr(w, err)
		return
	}
	out := make([]actionItemDTO, 0, len(rows))
	for _, a := range rows {
		out = append(out, toActionItemDTO(a))
	}
	writeJSON(w, http.StatusOK, out)
}

// loadItemAndIncident fetches an action item and its incident (locked) inside
// a transaction, and verifies the caller is the assigned responder.
func (s *Server) loadOwnedItem(w http.ResponseWriter, r *http.Request, q *store.Queries, actionItemID uuid.UUID) (store.ActionItem, store.Incident, bool) {
	u := actor(r)
	item, err := q.GetActionItemForUpdate(r.Context(), pgUUID(actionItemID))
	if isNotFound(err) {
		writeErr(w, errNotFound)
		return item, store.Incident{}, false
	}
	if err != nil {
		writeErr(w, err)
		return item, store.Incident{}, false
	}
	inc, err := q.GetIncidentForUpdate(r.Context(), item.IncidentID)
	if err != nil {
		writeErr(w, err)
		return item, store.Incident{}, false
	}
	if ae := requireAssignee(u, inc); ae != nil {
		writeErr(w, ae)
		return item, store.Incident{}, false
	}
	return item, inc, true
}

type rescheduleRequest struct {
	DueAt time.Time `json:"dueAt"`
}

// handleRescheduleActionItem moves the due date and bumps due_version. Any
// reminder scheduled for the old version can no longer fire, because the
// scheduler only delivers while (due_version, due_at) still match.
func (s *Server) handleRescheduleActionItem(w http.ResponseWriter, r *http.Request) {
	itemID, ae := urlUUID(r, "actionItemID")
	if ae != nil {
		writeErr(w, ae)
		return
	}
	var req rescheduleRequest
	if ae := decodeJSON(r, &req); ae != nil {
		writeErr(w, ae)
		return
	}
	if req.DueAt.IsZero() {
		writeErr(w, errOf(http.StatusBadRequest, "validation", "dueAt is required"))
		return
	}

	tx, err := s.pool.Begin(r.Context())
	if err != nil {
		writeErr(w, err)
		return
	}
	defer tx.Rollback(r.Context())
	q := s.q.WithTx(tx)

	item, inc, ok := s.loadOwnedItem(w, r, q, itemID)
	if !ok {
		return
	}
	if item.Status != "open" {
		writeErr(w, errOf(http.StatusConflict, "invalid_transition", "only open action items can be rescheduled"))
		return
	}
	item, err = q.RescheduleActionItem(r.Context(), store.RescheduleActionItemParams{
		ID:    pgUUID(itemID),
		DueAt: pgtypeTimestamptz(req.DueAt),
	})
	if err != nil {
		writeErr(w, err)
		return
	}
	u := actor(r)
	if err := insertAudit(r, q, inc.ID, &u.ID, "action_item.rescheduled", map[string]any{
		"actionItemId": itemID.String(),
		"dueAt":        req.DueAt.UTC().Format(time.RFC3339),
		"dueVersion":   item.DueVersion,
	}); err != nil {
		writeErr(w, err)
		return
	}
	if err := tx.Commit(r.Context()); err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, toActionItemDTO(item))
}

func (s *Server) handleCompleteActionItem(w http.ResponseWriter, r *http.Request) {
	itemID, ae := urlUUID(r, "actionItemID")
	if ae != nil {
		writeErr(w, ae)
		return
	}

	tx, err := s.pool.Begin(r.Context())
	if err != nil {
		writeErr(w, err)
		return
	}
	defer tx.Rollback(r.Context())
	q := s.q.WithTx(tx)

	item, inc, ok := s.loadOwnedItem(w, r, q, itemID)
	if !ok {
		return
	}
	if item.Status != "open" {
		writeErr(w, errOf(http.StatusConflict, "invalid_transition", "action item is already done"))
		return
	}
	item, err = q.CompleteActionItem(r.Context(), pgUUID(itemID))
	if err != nil {
		writeErr(w, err)
		return
	}
	u := actor(r)
	if err := insertAudit(r, q, inc.ID, &u.ID, "action_item.completed",
		map[string]any{"actionItemId": itemID.String()}); err != nil {
		writeErr(w, err)
		return
	}
	if err := tx.Commit(r.Context()); err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, toActionItemDTO(item))
}
