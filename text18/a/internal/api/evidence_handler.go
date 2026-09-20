package api

import (
	"net/http"

	"github.com/go-chi/chi/v5"

	"sircc/internal/auth"
	"sircc/internal/service"
)

type addEvidenceRequest struct {
	Content string `json:"content"`
}

func (h *Handlers) addEvidence(w http.ResponseWriter, r *http.Request) {
	incidentID, ok := parseUUID(w, chi.URLParam(r, "incidentID"))
	if !ok {
		return
	}
	var req addEvidenceRequest
	if !decode(w, r, &req) {
		return
	}
	res, err := h.svc.AddEvidence(r.Context(), auth.User(r.Context()), incidentID, service.AddEvidenceInput{
		Content:   req.Content,
		RequestID: requestID(r),
	})
	if err != nil {
		writeError(w, err)
		return
	}
	renderResult(w, res)
}

func (h *Handlers) listEvidence(w http.ResponseWriter, r *http.Request) {
	incidentID, ok := parseUUID(w, chi.URLParam(r, "incidentID"))
	if !ok {
		return
	}
	items, err := h.svc.ListEvidence(r.Context(), auth.User(r.Context()), incidentID)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"evidence": items, "count": len(items)})
}

type addNoteRequest struct {
	Note string `json:"note"`
}

func (h *Handlers) addEvidenceNote(w http.ResponseWriter, r *http.Request) {
	incidentID, ok := parseUUID(w, chi.URLParam(r, "incidentID"))
	if !ok {
		return
	}
	evidenceID, ok := parseUUID(w, chi.URLParam(r, "evidenceID"))
	if !ok {
		return
	}
	var req addNoteRequest
	if !decode(w, r, &req) {
		return
	}
	note, err := h.svc.AddEvidenceNote(r.Context(), auth.User(r.Context()),
		incidentID, evidenceID, req.Note)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, note)
}
