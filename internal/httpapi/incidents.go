package httpapi

import (
	"encoding/json"
	"fmt"
	"net/http"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgtype"

	"sircc/internal/store"
)

// statusOrder defines the linear incident lifecycle. A transition is legal
// only from status s to nextStatus[s]; skipping phases is impossible.
var nextStatus = map[string]string{
	"detected":   "triaged",
	"triaged":    "contained",
	"contained":  "eradicated",
	"eradicated": "recovered",
	"recovered":  "postmortem",
	"postmortem": "closed",
}

// ---- create / read ----

type createIncidentRequest struct {
	Title       string `json:"title"`
	Description string `json:"description"`
	Severity    string `json:"severity"`
}

func (s *Server) handleCreateIncident(w http.ResponseWriter, r *http.Request) {
	var req createIncidentRequest
	if ae := decodeJSON(r, &req); ae != nil {
		writeErr(w, ae)
		return
	}
	if req.Title == "" {
		writeErr(w, errOf(http.StatusBadRequest, "validation", "title is required"))
		return
	}
	switch req.Severity {
	case "P1", "P2", "P3", "P4":
	default:
		writeErr(w, errOf(http.StatusBadRequest, "validation", "severity must be one of P1..P4"))
		return
	}
	u := actor(r)

	tx, err := s.pool.Begin(r.Context())
	if err != nil {
		writeErr(w, err)
		return
	}
	defer tx.Rollback(r.Context())
	q := s.q.WithTx(tx)

	inc, err := q.CreateIncident(r.Context(), store.CreateIncidentParams{
		ID:          pgUUID(uuid.New()),
		Title:       req.Title,
		Description: req.Description,
		Severity:    req.Severity,
		CreatedBy:   u.ID,
	})
	if err != nil {
		writeErr(w, err)
		return
	}
	if err := insertAudit(r, q, inc.ID, &u.ID, "incident.created",
		map[string]any{"title": inc.Title, "severity": inc.Severity}); err != nil {
		writeErr(w, err)
		return
	}
	if err := tx.Commit(r.Context()); err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, toIncidentDTO(inc))
}

func (s *Server) handleListIncidents(w http.ResponseWriter, r *http.Request) {
	rows, err := s.q.ListIncidents(r.Context())
	if err != nil {
		writeErr(w, err)
		return
	}
	out := make([]incidentDTO, 0, len(rows))
	for _, inc := range rows {
		out = append(out, toIncidentDTO(inc))
	}
	writeJSON(w, http.StatusOK, out)
}

func (s *Server) handleGetIncident(w http.ResponseWriter, r *http.Request) {
	id, ae := urlUUID(r, "incidentID")
	if ae != nil {
		writeErr(w, ae)
		return
	}
	inc, err := s.q.GetIncident(r.Context(), pgUUID(id))
	if isNotFound(err) {
		writeErr(w, errNotFound)
		return
	}
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, toIncidentDTO(inc))
}

// ---- assignment (admin only) ----

type assignRequest struct {
	ResponderID uuid.UUID `json:"responderId"`
}

func (s *Server) handleAssign(w http.ResponseWriter, r *http.Request) {
	u := actor(r)
	if ae := requireRole(u, "admin"); ae != nil {
		writeErr(w, ae)
		return
	}
	id, ae := urlUUID(r, "incidentID")
	if ae != nil {
		writeErr(w, ae)
		return
	}
	var req assignRequest
	if ae := decodeJSON(r, &req); ae != nil {
		writeErr(w, ae)
		return
	}
	target, err := s.q.GetUser(r.Context(), pgUUID(req.ResponderID))
	if isNotFound(err) {
		writeErr(w, errOf(http.StatusBadRequest, "validation", "responder user not found"))
		return
	}
	if err != nil {
		writeErr(w, err)
		return
	}
	if target.Role != "responder" {
		writeErr(w, errOf(http.StatusBadRequest, "validation", "assignee must have the responder role"))
		return
	}

	tx, err := s.pool.Begin(r.Context())
	if err != nil {
		writeErr(w, err)
		return
	}
	defer tx.Rollback(r.Context())
	q := s.q.WithTx(tx)

	if _, err := q.GetIncidentForUpdate(r.Context(), pgUUID(id)); isNotFound(err) {
		writeErr(w, errNotFound)
		return
	} else if err != nil {
		writeErr(w, err)
		return
	}
	inc, err := q.AssignResponder(r.Context(), store.AssignResponderParams{
		ID:         pgUUID(id),
		AssigneeID: pgUUID(req.ResponderID),
	})
	if err != nil {
		writeErr(w, err)
		return
	}
	if err := insertAudit(r, q, inc.ID, &u.ID, "incident.assigned",
		map[string]any{"assigneeId": req.ResponderID.String()}); err != nil {
		writeErr(w, err)
		return
	}
	if err := tx.Commit(r.Context()); err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, toIncidentDTO(inc))
}

// ---- phase transitions ----

type transitionRequest struct {
	ToStatus        string `json:"toStatus"`
	ExpectedVersion int64  `json:"expectedVersion"`
	RequestID       string `json:"requestId"`
	Note            string `json:"note"`
}

// handleTransition applies one lifecycle step. The incident row is locked for
// the whole transaction; the status update, the phase record and the audit
// event commit or roll back together, so a failed transition can never leave
// a half-updated incident.
func (s *Server) handleTransition(w http.ResponseWriter, r *http.Request) {
	u := actor(r)
	id, ae := urlUUID(r, "incidentID")
	if ae != nil {
		writeErr(w, ae)
		return
	}
	var req transitionRequest
	if ae := decodeJSON(r, &req); ae != nil {
		writeErr(w, ae)
		return
	}
	if req.RequestID == "" {
		writeErr(w, errOf(http.StatusBadRequest, "validation", "requestId is required"))
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

	// Idempotency: a replayed requestId returns the originally recorded
	// result without re-validating or re-applying anything.
	if prev, err := q.GetTransitionByRequestID(r.Context(), store.GetTransitionByRequestIDParams{
		IncidentID: pgUUID(id),
		RequestID:  req.RequestID,
	}); err == nil {
		if err := tx.Commit(r.Context()); err != nil {
			writeErr(w, err)
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"transition": toTransitionDTO(prev),
			"replayed":   true,
		})
		return
	}

	// Authorization: analysts triage; the assigned responder drives the rest.
	if req.ToStatus == "triaged" {
		if ae := requireRole(u, "analyst"); ae != nil {
			writeErr(w, ae)
			return
		}
	} else {
		if ae := requireAssignee(u, inc); ae != nil {
			writeErr(w, ae)
			return
		}
	}

	// Optimistic concurrency: the caller must have seen the current version.
	if inc.Version != req.ExpectedVersion {
		writeErr(w, errOf(http.StatusConflict, "version_conflict",
			fmt.Sprintf("expected version %d, current version is %d", req.ExpectedVersion, inc.Version)))
		return
	}

	// Lifecycle order: exactly one step forward, never a skip or a move back.
	if nextStatus[inc.Status] != req.ToStatus {
		writeErr(w, errOf(http.StatusConflict, "invalid_transition",
			fmt.Sprintf("cannot move from %s to %s", inc.Status, req.ToStatus)))
		return
	}

	// Phase gates.
	if req.ToStatus == "triaged" && inc.Severity == "P1" && !inc.AssigneeID.Valid {
		writeErr(w, errOf(http.StatusUnprocessableEntity, "triage_gate",
			"a P1 incident must have an assigned responder before triage completes"))
		return
	}
	if req.ToStatus == "closed" {
		if inc.RootCause == "" || inc.LessonsLearned == "" {
			writeErr(w, errOf(http.StatusUnprocessableEntity, "close_gate",
				"root cause and lessons learned are required before closing"))
			return
		}
		n, err := q.CountActionItems(r.Context(), pgUUID(id))
		if err != nil {
			writeErr(w, err)
			return
		}
		if n == 0 {
			writeErr(w, errOf(http.StatusUnprocessableEntity, "close_gate",
				"at least one action item with an owner and a due date is required before closing"))
			return
		}
	}

	newVersion := inc.Version + 1
	if _, err := q.UpdateIncidentStatus(r.Context(), store.UpdateIncidentStatusParams{
		ID:      pgUUID(id),
		Status:  req.ToStatus,
		Version: newVersion,
	}); err != nil {
		writeErr(w, err)
		return
	}
	tr, err := q.InsertTransition(r.Context(), store.InsertTransitionParams{
		ID:           pgUUID(uuid.New()),
		IncidentID:   pgUUID(id),
		RequestID:    req.RequestID,
		FromStatus:   inc.Status,
		ToStatus:     req.ToStatus,
		ActorID:      u.ID,
		Note:         req.Note,
		VersionAfter: newVersion,
	})
	if err != nil {
		writeErr(w, err)
		return
	}
	if err := insertAudit(r, q, pgUUID(id), &u.ID, "phase.transition", map[string]any{
		"fromStatus": inc.Status, "toStatus": req.ToStatus,
		"requestId": req.RequestID, "versionAfter": newVersion,
	}); err != nil {
		writeErr(w, err)
		return
	}
	if err := tx.Commit(r.Context()); err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"transition": toTransitionDTO(tr),
		"replayed":   false,
	})
}

// ---- postmortem fields ----

type postmortemRequest struct {
	RootCause      string `json:"rootCause"`
	LessonsLearned string `json:"lessonsLearned"`
}

func (s *Server) handleUpdatePostmortem(w http.ResponseWriter, r *http.Request) {
	u := actor(r)
	id, ae := urlUUID(r, "incidentID")
	if ae != nil {
		writeErr(w, ae)
		return
	}
	var req postmortemRequest
	if ae := decodeJSON(r, &req); ae != nil {
		writeErr(w, ae)
		return
	}
	if req.RootCause == "" || req.LessonsLearned == "" {
		writeErr(w, errOf(http.StatusBadRequest, "validation", "rootCause and lessonsLearned are required"))
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
	if inc.Status != "postmortem" {
		writeErr(w, errOf(http.StatusConflict, "invalid_transition",
			"postmortem fields can only be edited while the incident is in postmortem"))
		return
	}
	inc, err = q.UpdatePostmortem(r.Context(), store.UpdatePostmortemParams{
		ID:             pgUUID(id),
		RootCause:      req.RootCause,
		LessonsLearned: req.LessonsLearned,
	})
	if err != nil {
		writeErr(w, err)
		return
	}
	if err := insertAudit(r, q, pgUUID(id), &u.ID, "postmortem.updated", map[string]any{}); err != nil {
		writeErr(w, err)
		return
	}
	if err := tx.Commit(r.Context()); err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, toIncidentDTO(inc))
}

// ---- audit ----

func (s *Server) handleListAudit(w http.ResponseWriter, r *http.Request) {
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
	rows, err := s.q.ListAuditEventsByIncident(r.Context(), pgUUID(id))
	if err != nil {
		writeErr(w, err)
		return
	}
	out := make([]auditEventDTO, 0, len(rows))
	for _, e := range rows {
		out = append(out, toAuditEventDTO(e))
	}
	writeJSON(w, http.StatusOK, out)
}

// insertAudit writes one audit event inside the caller's transaction.
func insertAudit(r *http.Request, q *store.Queries, incidentID pgtype.UUID, actorID *pgtype.UUID, eventType string, payload map[string]any) error {
	body, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	var actor pgtype.UUID
	if actorID != nil {
		actor = *actorID
	}
	_, err = q.InsertAuditEvent(r.Context(), store.InsertAuditEventParams{
		ID:         pgUUID(uuid.New()),
		IncidentID: incidentID,
		ActorID:    actor,
		EventType:  eventType,
		Payload:    body,
	})
	return err
}
