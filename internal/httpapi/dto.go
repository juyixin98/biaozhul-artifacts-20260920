package httpapi

import (
	"encoding/json"
	"net/http"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgtype"

	"sircc/internal/store"
)

// ---- error responses ----

type apiError struct {
	Status  int
	Code    string
	Message string
}

func (e *apiError) Error() string { return e.Message }

func errOf(status int, code, msg string) *apiError {
	return &apiError{Status: status, Code: code, Message: msg}
}

var (
	errUnauthenticated = errOf(http.StatusUnauthorized, "unauthenticated", "missing or unknown X-User-Id header")
	errForbidden       = errOf(http.StatusForbidden, "forbidden", "operation not permitted for this role or case")
	errNotFound        = errOf(http.StatusNotFound, "not_found", "resource not found")
)

func writeErr(w http.ResponseWriter, err error) {
	if ae, ok := err.(*apiError); ok {
		writeJSON(w, ae.Status, map[string]any{
			"error": map[string]string{"code": ae.Code, "message": ae.Message},
		})
		return
	}
	writeJSON(w, http.StatusInternalServerError, map[string]any{
		"error": map[string]string{"code": "internal", "message": "internal error"},
	})
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(v)
}

func decodeJSON(r *http.Request, dst any) *apiError {
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(dst); err != nil {
		return errOf(http.StatusBadRequest, "validation", "invalid JSON body: "+err.Error())
	}
	return nil
}

// ---- uuid helpers ----

func pgUUID(id uuid.UUID) pgtype.UUID { return pgtype.UUID{Bytes: id, Valid: true} }

func pgtypeTimestamptz(t time.Time) pgtype.Timestamptz {
	return pgtype.Timestamptz{Time: t, Valid: true}
}

func fromPGUUID(id pgtype.UUID) uuid.UUID { return uuid.UUID(id.Bytes) }

func parseUUID(s string) (uuid.UUID, *apiError) {
	id, err := uuid.Parse(s)
	if err != nil {
		return uuid.Nil, errOf(http.StatusBadRequest, "validation", "invalid uuid: "+s)
	}
	return id, nil
}

// ---- DTOs ----

type incidentDTO struct {
	ID             uuid.UUID  `json:"id"`
	Title          string     `json:"title"`
	Description    string     `json:"description"`
	Severity       string     `json:"severity"`
	Status         string     `json:"status"`
	Version        int64      `json:"version"`
	AssigneeID     *uuid.UUID `json:"assigneeId"`
	RootCause      string     `json:"rootCause"`
	LessonsLearned string     `json:"lessonsLearned"`
	CreatedBy      uuid.UUID  `json:"createdBy"`
	CreatedAt      time.Time  `json:"createdAt"`
	UpdatedAt      time.Time  `json:"updatedAt"`
}

func toIncidentDTO(i store.Incident) incidentDTO {
	d := incidentDTO{
		ID:             fromPGUUID(i.ID),
		Title:          i.Title,
		Description:    i.Description,
		Severity:       i.Severity,
		Status:         i.Status,
		Version:        i.Version,
		RootCause:      i.RootCause,
		LessonsLearned: i.LessonsLearned,
		CreatedBy:      fromPGUUID(i.CreatedBy),
		CreatedAt:      i.CreatedAt.Time,
		UpdatedAt:      i.UpdatedAt.Time,
	}
	if i.AssigneeID.Valid {
		a := fromPGUUID(i.AssigneeID)
		d.AssigneeID = &a
	}
	return d
}

type transitionDTO struct {
	ID           uuid.UUID `json:"id"`
	IncidentID   uuid.UUID `json:"incidentId"`
	RequestID    string    `json:"requestId"`
	FromStatus   string    `json:"fromStatus"`
	ToStatus     string    `json:"toStatus"`
	ActorID      uuid.UUID `json:"actorId"`
	Note         string    `json:"note"`
	VersionAfter int64     `json:"versionAfter"`
	CreatedAt    time.Time `json:"createdAt"`
}

func toTransitionDTO(t store.PhaseTransition) transitionDTO {
	return transitionDTO{
		ID:           fromPGUUID(t.ID),
		IncidentID:   fromPGUUID(t.IncidentID),
		RequestID:    t.RequestID,
		FromStatus:   t.FromStatus,
		ToStatus:     t.ToStatus,
		ActorID:      fromPGUUID(t.ActorID),
		Note:         t.Note,
		VersionAfter: t.VersionAfter,
		CreatedAt:    t.CreatedAt.Time,
	}
}

type evidenceDTO struct {
	ID         uuid.UUID `json:"id"`
	IncidentID uuid.UUID `json:"incidentId"`
	Seq        int32     `json:"seq"`
	Content    string    `json:"content"`
	AuthorID   uuid.UUID `json:"authorId"`
	CreatedAt  time.Time `json:"createdAt"`
}

func toEvidenceDTO(e store.Evidence) evidenceDTO {
	return evidenceDTO{
		ID:         fromPGUUID(e.ID),
		IncidentID: fromPGUUID(e.IncidentID),
		Seq:        e.Seq,
		Content:    e.Content,
		AuthorID:   fromPGUUID(e.AuthorID),
		CreatedAt:  e.CreatedAt.Time,
	}
}

type evidenceNoteDTO struct {
	ID         uuid.UUID `json:"id"`
	EvidenceID uuid.UUID `json:"evidenceId"`
	Content    string    `json:"content"`
	AuthorID   uuid.UUID `json:"authorId"`
	CreatedAt  time.Time `json:"createdAt"`
}

func toEvidenceNoteDTO(n store.EvidenceNote) evidenceNoteDTO {
	return evidenceNoteDTO{
		ID:         fromPGUUID(n.ID),
		EvidenceID: fromPGUUID(n.EvidenceID),
		Content:    n.Content,
		AuthorID:   fromPGUUID(n.AuthorID),
		CreatedAt:  n.CreatedAt.Time,
	}
}

type actionItemDTO struct {
	ID           uuid.UUID `json:"id"`
	IncidentID   uuid.UUID `json:"incidentId"`
	Title        string    `json:"title"`
	OwnerID      uuid.UUID `json:"ownerId"`
	DueAt        time.Time `json:"dueAt"`
	Status       string    `json:"status"`
	DueVersion   int64     `json:"dueVersion"`
	RemindedUpTo int64     `json:"remindedDueVersion"`
	CreatedAt    time.Time `json:"createdAt"`
	UpdatedAt    time.Time `json:"updatedAt"`
}

func toActionItemDTO(a store.ActionItem) actionItemDTO {
	return actionItemDTO{
		ID:           fromPGUUID(a.ID),
		IncidentID:   fromPGUUID(a.IncidentID),
		Title:        a.Title,
		OwnerID:      fromPGUUID(a.OwnerID),
		DueAt:        a.DueAt.Time,
		Status:       a.Status,
		DueVersion:   a.DueVersion,
		RemindedUpTo: a.RemindedDueVersion,
		CreatedAt:    a.CreatedAt.Time,
		UpdatedAt:    a.UpdatedAt.Time,
	}
}

type auditEventDTO struct {
	ID         uuid.UUID      `json:"id"`
	IncidentID uuid.UUID      `json:"incidentId"`
	ActorID    *uuid.UUID     `json:"actorId"`
	EventType  string         `json:"eventType"`
	Payload    map[string]any `json:"payload"`
	CreatedAt  time.Time      `json:"createdAt"`
}

func toAuditEventDTO(e store.AuditEvent) auditEventDTO {
	d := auditEventDTO{
		ID:         fromPGUUID(e.ID),
		IncidentID: fromPGUUID(e.IncidentID),
		EventType:  e.EventType,
		CreatedAt:  e.CreatedAt.Time,
	}
	if e.ActorID.Valid {
		a := fromPGUUID(e.ActorID)
		d.ActorID = &a
	}
	if len(e.Payload) > 0 {
		_ = json.Unmarshal(e.Payload, &d.Payload)
	}
	if d.Payload == nil {
		d.Payload = map[string]any{}
	}
	return d
}
