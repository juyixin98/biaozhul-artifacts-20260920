package server

import (
	"errors"
	"net/http"
	"strconv"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/jackc/pgx/v5"

	"dams.local/dams/internal/auditchain"
	sqlcgen "dams.local/dams/internal/db/sqlc"
)

// allowedTransitions defines the alert state machine:
// open -> investigating | resolved | false_positive
// investigating -> resolved | false_positive | open (back to triage)
// resolved/false_positive are terminal.
var allowedTransitions = map[string]map[string]bool{
	"open": {
		"investigating": true, "resolved": true, "false_positive": true,
	},
	"investigating": {
		"resolved": true, "false_positive": true, "open": true,
	},
}

type transitionRequest struct {
	ToStatus        string `json:"to_status"`
	ExpectedVersion int32  `json:"expected_version"`
	Note            string `json:"note"`
}

type assignRequest struct {
	ExpectedVersion int32 `json:"expected_version"`
}

type recomputeRequest struct {
	// Optional explicit evidence note; defaults to analyst-triggered recompute.
	Note string `json:"note"`
}

func (s *Server) handleListAlerts(w http.ResponseWriter, r *http.Request) {
	org := currentOrg(r)
	status := r.URL.Query().Get("status")
	if status != "" && status != "open" && status != "investigating" &&
		status != "resolved" && status != "false_positive" {
		writeError(w, http.StatusBadRequest, "bad_status", "unsupported status filter", nil)
		return
	}
	limit := int32(100)
	offset := int32(0)
	if v := r.URL.Query().Get("limit"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 && n <= 500 {
			limit = int32(n)
		}
	}
	if v := r.URL.Query().Get("offset"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n >= 0 {
			offset = int32(n)
		}
	}
	var statusArg *string
	if status != "" {
		statusArg = &status
	}
	alerts, err := s.q.ListAlerts(r.Context(), sqlcgen.ListAlertsParams{
		OrgID: org.ID, Status: statusArg, Limit: limit, Offset: offset,
	})
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal_error", err.Error(), nil)
		return
	}
	total, err := s.q.CountAlerts(r.Context(), sqlcgen.CountAlertsParams{
		OrgID: org.ID, Status: statusArg,
	})
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal_error", err.Error(), nil)
		return
	}
	out := make([]map[string]any, 0, len(alerts))
	for _, a := range alerts {
		out = append(out, alertDTO(a))
	}
	writeJSON(w, http.StatusOK, map[string]any{"alerts": out, "total": total})
}

func (s *Server) handleGetAlert(w http.ResponseWriter, r *http.Request) {
	org := currentOrg(r)
	id, err := strconv.ParseInt(chi.URLParam(r, "id"), 10, 64)
	if err != nil {
		writeError(w, http.StatusBadRequest, "bad_id", "alert id must be an integer", nil)
		return
	}
	a, err := s.q.GetAlert(r.Context(), sqlcgen.GetAlertParams{OrgID: org.ID, ID: id})
	if errors.Is(err, pgx.ErrNoRows) {
		writeError(w, http.StatusNotFound, "not_found", "alert not found", nil)
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal_error", err.Error(), nil)
		return
	}
	writeJSON(w, http.StatusOK, alertDTO(a))
}

func (s *Server) handleListRevisions(w http.ResponseWriter, r *http.Request) {
	org := currentOrg(r)
	id, err := strconv.ParseInt(chi.URLParam(r, "id"), 10, 64)
	if err != nil {
		writeError(w, http.StatusBadRequest, "bad_id", "alert id must be an integer", nil)
		return
	}
	if _, err := s.q.GetAlert(r.Context(), sqlcgen.GetAlertParams{OrgID: org.ID, ID: id}); errors.Is(err, pgx.ErrNoRows) {
		writeError(w, http.StatusNotFound, "not_found", "alert not found", nil)
		return
	}
	revs, err := s.q.ListAlertRevisions(r.Context(), id)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal_error", err.Error(), nil)
		return
	}
	out := make([]map[string]any, 0, len(revs))
	for _, rv := range revs {
		out = append(out, revisionDTO(rv))
	}
	writeJSON(w, http.StatusOK, map[string]any{"revisions": out})
}

func (s *Server) handleTransitionAlert(w http.ResponseWriter, r *http.Request) {
	org := currentOrg(r)
	p := principal(r)
	id, err := strconv.ParseInt(chi.URLParam(r, "id"), 10, 64)
	if err != nil {
		writeError(w, http.StatusBadRequest, "bad_id", "alert id must be an integer", nil)
		return
	}
	var req transitionRequest
	if err := decodeJSON(w, r, &req, 1<<20); err != nil {
		writeError(w, http.StatusBadRequest, "bad_json", "invalid JSON body: "+err.Error(), nil)
		return
	}
	if req.ExpectedVersion < 1 {
		writeError(w, http.StatusBadRequest, "expected_version",
			"expected_version (current alert version) is required", nil)
		return
	}

	var updated sqlcgen.Alert
	err = runTx(r.Context(), s.pool, func(q *sqlcgen.Queries, tx pgx.Tx) error {
		a, gErr := q.GetAlert(r.Context(), sqlcgen.GetAlertParams{OrgID: org.ID, ID: id})
		if errors.Is(gErr, pgx.ErrNoRows) {
			writeError(w, http.StatusNotFound, "not_found", "alert not found", nil)
			return errHandled
		}
		if gErr != nil {
			return gErr
		}
		if a.Version != req.ExpectedVersion {
			writeError(w, http.StatusConflict, "version_conflict",
				"alert was modified since it was read",
				map[string]any{"current_version": a.Version, "status": a.Status})
			return errHandled
		}
		if !allowedTransitions[a.Status][req.ToStatus] {
			writeError(w, http.StatusConflict, "illegal_transition",
				"cannot move alert from "+a.Status+" to "+req.ToStatus,
				map[string]any{"current_status": a.Status})
			return errHandled
		}
		from := a.Status
		updated, gErr = q.TransitionAlert(r.Context(), sqlcgen.TransitionAlertParams{
			ID: id, OrgID: org.ID, Status: req.ToStatus,
			AssignedTo: nil, Version: req.ExpectedVersion,
		})
		if errors.Is(gErr, pgx.ErrNoRows) {
			// Another request changed the alert between the check and the
			// update (optimistic-lock predicate lost).
			cur, _ := q.GetAlert(r.Context(), sqlcgen.GetAlertParams{OrgID: org.ID, ID: id})
			writeError(w, http.StatusConflict, "version_conflict",
				"alert was modified concurrently",
				map[string]any{"current_version": cur.Version, "status": cur.Status})
			return errHandled
		}
		if gErr != nil {
			return gErr
		}
		if gErr := q.InsertAlertRevision(r.Context(), sqlcgen.InsertAlertRevisionParams{
			AlertID: id, Revision: "transition",
			FromStatus: &from, ToStatus: &req.ToStatus,
			Note: req.Note, EventCount: nil, ActorID: &p.UserID,
		}); gErr != nil {
			return gErr
		}
		_, _, aErr := auditchain.Append(r.Context(), tx, q, org.ID, p.UserID,
			"alert.transition", map[string]any{
				"alert_id": id, "from": from, "to": req.ToStatus,
				"note": req.Note,
			})
		return aErr
	})
	if errors.Is(err, errHandled) {
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, "transition_failed", err.Error(), nil)
		return
	}
	writeJSON(w, http.StatusOK, alertDTO(updated))
}

func (s *Server) handleAssignAlert(w http.ResponseWriter, r *http.Request) {
	org := currentOrg(r)
	p := principal(r)
	id, err := strconv.ParseInt(chi.URLParam(r, "id"), 10, 64)
	if err != nil {
		writeError(w, http.StatusBadRequest, "bad_id", "alert id must be an integer", nil)
		return
	}
	var req assignRequest
	if err := decodeJSON(w, r, &req, 1<<20); err != nil {
		writeError(w, http.StatusBadRequest, "bad_json", "invalid JSON body: "+err.Error(), nil)
		return
	}
	if req.ExpectedVersion < 1 {
		writeError(w, http.StatusBadRequest, "expected_version",
			"expected_version (current alert version) is required", nil)
		return
	}

	var updated sqlcgen.Alert
	err = runTx(r.Context(), s.pool, func(q *sqlcgen.Queries, tx pgx.Tx) error {
		a, gErr := q.GetAlert(r.Context(), sqlcgen.GetAlertParams{OrgID: org.ID, ID: id})
		if errors.Is(gErr, pgx.ErrNoRows) {
			writeError(w, http.StatusNotFound, "not_found", "alert not found", nil)
			return errHandled
		}
		if gErr != nil {
			return gErr
		}
		if a.Version != req.ExpectedVersion {
			writeError(w, http.StatusConflict, "version_conflict",
				"alert was modified since it was read",
				map[string]any{"current_version": a.Version, "status": a.Status})
			return errHandled
		}
		updated, gErr = q.AssignAlert(r.Context(), sqlcgen.AssignAlertParams{
			ID: id, OrgID: org.ID, AssignedTo: &p.UserID, Version: req.ExpectedVersion,
		})
		if errors.Is(gErr, pgx.ErrNoRows) {
			cur, _ := q.GetAlert(r.Context(), sqlcgen.GetAlertParams{OrgID: org.ID, ID: id})
			writeError(w, http.StatusConflict, "version_conflict",
				"alert was modified concurrently",
				map[string]any{"current_version": cur.Version, "status": cur.Status})
			return errHandled
		}
		if gErr != nil {
			return gErr
		}
		if gErr := q.InsertAlertRevision(r.Context(), sqlcgen.InsertAlertRevisionParams{
			AlertID: id, Revision: "assign",
			Note:    "self-assigned for investigation",
			ActorID: &p.UserID,
		}); gErr != nil {
			return gErr
		}
		_, _, aErr := auditchain.Append(r.Context(), tx, q, org.ID, p.UserID,
			"alert.assign", map[string]any{"alert_id": id, "assignee": p.UserID})
		return aErr
	})
	if errors.Is(err, errHandled) {
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, "assign_failed", err.Error(), nil)
		return
	}
	writeJSON(w, http.StatusOK, alertDTO(updated))
}

// handleRecomputeAlert triggers evidence recomputation for a rate alert.
// It appends only evidence (alert_events links, counter, 'recompute'
// revision); it never changes status, assignment or analyst notes.
func (s *Server) handleRecomputeAlert(w http.ResponseWriter, r *http.Request) {
	org := currentOrg(r)
	p := principal(r)
	id, err := strconv.ParseInt(chi.URLParam(r, "id"), 10, 64)
	if err != nil {
		writeError(w, http.StatusBadRequest, "bad_id", "alert id must be an integer", nil)
		return
	}
	var req recomputeRequest
	if r.ContentLength != 0 {
		if err := decodeJSON(w, r, &req, 1<<20); err != nil {
			writeError(w, http.StatusBadRequest, "bad_json", "invalid JSON body: "+err.Error(), nil)
			return
		}
	}

	var dto map[string]any
	err = runTx(r.Context(), s.pool, func(q *sqlcgen.Queries, tx pgx.Tx) error {
		a, gErr := q.GetAlert(r.Context(), sqlcgen.GetAlertParams{OrgID: org.ID, ID: id})
		if errors.Is(gErr, pgx.ErrNoRows) {
			writeError(w, http.StatusNotFound, "not_found", "alert not found", nil)
			return errHandled
		}
		if gErr != nil {
			return gErr
		}
		if a.Kind != "rate" {
			writeError(w, http.StatusUnprocessableEntity, "not_recomputable",
				"only rate alerts support window recomputation", nil)
			return errHandled
		}
		before := a.EventCount

		rows, gErr := q.EventsInWindow(r.Context(), sqlcgen.EventsInWindowParams{
			OrgID: org.ID, DbUser: deref(a.DbUser),
			OccurredAt: a.WindowStart, OccurredAt_2: a.WindowEnd,
		})
		if gErr != nil {
			return gErr
		}
		var added int
		for _, e := range rows {
			if gErr := q.LinkAlertEvent(r.Context(),
				sqlcgen.LinkAlertEventParams{AlertID: id, EventPk: e.ID}); gErr != nil {
				return gErr
			}
		}
		linked, gErr := q.CountAlertEvents(r.Context(), id)
		if gErr != nil {
			return gErr
		}
		if linked != a.EventCount {
			if gErr := q.BumpAlertCount(r.Context(), sqlcgen.BumpAlertCountParams{
				OrgID: org.ID, ID: id, EventCount: linked,
			}); gErr != nil {
				return gErr
			}
			added = int(linked) - int(before)
			note := req.Note
			if note == "" {
				note = "manual recompute"
			}
			note += " (" + strconv.Itoa(added) + " new late/out-of-order events)"
			c := linked
			if gErr := q.InsertAlertRevision(r.Context(), sqlcgen.InsertAlertRevisionParams{
				AlertID: id, Revision: "recompute",
				Note: note, EventCount: &c, ActorID: &p.UserID,
			}); gErr != nil {
				return gErr
			}
			_, _, aErr := auditchain.Append(r.Context(), tx, q, org.ID, p.UserID,
				"alert.recompute", map[string]any{
					"alert_id": id, "rule_version": a.RuleVersion,
					"before": before, "after": linked, "added": added,
				})
			if aErr != nil {
				return aErr
			}
			a.EventCount = linked
		}
		dto = alertDTO(a)
		return nil
	})
	if errors.Is(err, errHandled) {
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, "recompute_failed", err.Error(), nil)
		return
	}
	writeJSON(w, http.StatusOK, dto)
}

func deref(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}

func alertDTO(a sqlcgen.Alert) map[string]any {
	return map[string]any{
		"id": a.ID, "org_id": a.OrgID,
		"rule_id": a.RuleID, "rule_version": a.RuleVersion,
		"kind": a.Kind, "status": a.Status,
		"fingerprint":  a.Fingerprint,
		"window_start": tstzOrNil(a.WindowStart),
		"window_end":   tstzOrNil(a.WindowEnd),
		"db_user":      a.DbUser, "event_id": a.EventID, "event_pk": a.EventPk,
		"event_count": a.EventCount, "assigned_to": a.AssignedTo,
		"version":    a.Version,
		"created_at": a.CreatedAt.Time.UTC().Format(time.RFC3339Nano),
		"updated_at": a.UpdatedAt.Time.UTC().Format(time.RFC3339Nano),
	}
}

func tstzOrNil(t pgtypeTimestamptz) any {
	if !t.Valid {
		return nil
	}
	return t.Time.UTC().Format(time.RFC3339Nano)
}

func revisionDTO(rv sqlcgen.ListAlertRevisionsRow) map[string]any {
	return map[string]any{
		"id": rv.ID, "revision": rv.Revision,
		"from_status": rv.FromStatus, "to_status": rv.ToStatus,
		"note": rv.Note, "event_count": rv.EventCount,
		"actor_id": rv.ActorID, "actor_name": rv.ActorName,
		"created_at": rv.CreatedAt.Time.UTC().Format(time.RFC3339Nano),
	}
}
