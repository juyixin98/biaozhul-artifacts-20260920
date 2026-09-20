package api

import (
	"net/http"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/google/uuid"

	"sircc/internal/auth"
	"sircc/internal/service"
)

type createActionItemRequest struct {
	Description string    `json:"description"`
	OwnerUserID string    `json:"owner_user_id"`
	DueAt       time.Time `json:"due_at"`
}

type rescheduleRequest struct {
	DueAt time.Time `json:"due_at"`
}

type setStatusRequest struct {
	Status string `json:"status"`
}

func (h *Handlers) createActionItem(w http.ResponseWriter, r *http.Request) {
	incidentID, ok := parseUUID(w, chi.URLParam(r, "incidentID"))
	if !ok {
		return
	}
	var req createActionItemRequest
	if !decode(w, r, &req) {
		return
	}
	ownerID, err := uuid.Parse(req.OwnerUserID)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, errorBody{
			Error: "validation_error", Message: "owner_user_id must be a UUID",
		})
		return
	}
	res, err := h.svc.CreateActionItem(r.Context(), auth.User(r.Context()), incidentID, service.CreateActionItemInput{
		Description: req.Description,
		OwnerUserID: ownerID,
		DueAt:       req.DueAt.UTC(),
		RequestID:   requestID(r),
	})
	if err != nil {
		writeError(w, err)
		return
	}
	renderResult(w, res)
}

func (h *Handlers) listActionItems(w http.ResponseWriter, r *http.Request) {
	incidentID, ok := parseUUID(w, chi.URLParam(r, "incidentID"))
	if !ok {
		return
	}
	items, reminders, err := h.svc.ListActionItems(r.Context(), auth.User(r.Context()), incidentID)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"action_items": items,
		"reminders":    reminders,
	})
}

func (h *Handlers) rescheduleActionItem(w http.ResponseWriter, r *http.Request) {
	incidentID, ok := parseUUID(w, chi.URLParam(r, "incidentID"))
	if !ok {
		return
	}
	itemID, ok := parseUUID(w, chi.URLParam(r, "itemID"))
	if !ok {
		return
	}
	var req rescheduleRequest
	if !decode(w, r, &req) {
		return
	}
	if req.DueAt.IsZero() {
		writeJSON(w, http.StatusBadRequest, errorBody{
			Error: "validation_error", Message: "due_at is required",
		})
		return
	}
	res, err := h.svc.RescheduleActionItem(r.Context(), auth.User(r.Context()),
		incidentID, itemID, service.RescheduleInput{
			DueAt:     req.DueAt.UTC(),
			RequestID: requestID(r),
		})
	if err != nil {
		writeError(w, err)
		return
	}
	renderResult(w, res)
}

func (h *Handlers) setActionItemStatus(w http.ResponseWriter, r *http.Request) {
	incidentID, ok := parseUUID(w, chi.URLParam(r, "incidentID"))
	if !ok {
		return
	}
	itemID, ok := parseUUID(w, chi.URLParam(r, "itemID"))
	if !ok {
		return
	}
	var req setStatusRequest
	if !decode(w, r, &req) {
		return
	}
	res, err := h.svc.SetActionItemStatus(r.Context(), auth.User(r.Context()),
		incidentID, itemID, req.Status)
	if err != nil {
		writeError(w, err)
		return
	}
	renderResult(w, res)
}
