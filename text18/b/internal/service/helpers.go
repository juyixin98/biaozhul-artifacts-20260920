package service

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"strconv"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"

	"sircc/internal/db"
	"sircc/internal/domain"
	"sircc/internal/httpx"
	"sircc/internal/middleware"
)

// ctxType keeps idemExec signatures short.
type ctxType = context.Context

func ts(t pgtype.Timestamptz) *time.Time {
	if !t.Valid {
		return nil
	}
	v := t.Time
	return &v
}

func mustJSON(v any) []byte {
	b, err := json.Marshal(v)
	if err != nil {
		return []byte("{}")
	}
	return b
}

func parsePagination(limitS, offsetS string) (int32, int32) {
	limit := int64(50)
	offset := int64(0)
	if n, err := strconv.ParseInt(limitS, 10, 64); err == nil && n > 0 && n <= 200 {
		limit = n
	}
	if n, err := strconv.ParseInt(offsetS, 10, 64); err == nil && n >= 0 {
		offset = n
	}
	return int32(limit), int32(offset)
}

// canRead enforces incident scope for read endpoints. It writes the error
// response itself and returns false when the incident is missing or the
// actor is not a member (admins bypass membership).
func (s *Service) canRead(w http.ResponseWriter, r *http.Request, actor middleware.Actor, incidentID string) bool {
	if _, err := s.q.GetIncident(r.Context(), incidentID); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			httpx.ErrorJSON(w, http.StatusNotFound, httpx.CodeNotFound, "incident not found")
			return false
		}
		httpx.ErrorJSON(w, http.StatusInternalServerError, httpx.CodeInternal, "load incident")
		return false
	}
	if actor.Role == domain.RoleAdmin {
		return true
	}
	member, err := s.q.IsIncidentMember(r.Context(), db.IsIncidentMemberParams{
		IncidentID: incidentID, UserID: actor.ID,
	})
	if err != nil {
		httpx.ErrorJSON(w, http.StatusInternalServerError, httpx.CodeInternal, "membership")
		return false
	}
	if !member {
		httpx.ErrorJSON(w, http.StatusForbidden, httpx.CodeForbidden, "not a member of this incident")
		return false
	}
	return true
}

// pstr converts a nullable pgtype.Text to a *string for JSON output.
func pstr(t pgtype.Text) *string {
	if !t.Valid || t.String == "" {
		return nil
	}
	v := t.String
	return &v
}

// textParam wraps a Go string as a nullable pgtype.Text.
func textParam(s string) pgtype.Text {
	return pgtype.Text{String: s, Valid: s != ""}
}

// tstz wraps a Go time as a pgtype.Timestamptz.
func tstz(t time.Time) pgtype.Timestamptz {
	return pgtype.Timestamptz{Time: t, Valid: !t.IsZero()}
}

// ptextFromT returns the pgtype.Text value's string/valid flag helper.
func textValid(t pgtype.Text) (string, bool) {
	return t.String, t.Valid && t.String != ""
}

func queryAuditParams(filter bool, incidentID string, limit, offset int32) db.ListAuditEventsParams {
	return db.ListAuditEventsParams{
		IncidentID:     textParam(incidentID),
		Limit:          limit,
		Offset:         offset,
		IncidentFilter: filter,
	}
}

func http500(w http.ResponseWriter, msg string) {
	httpx.ErrorJSON(w, http.StatusInternalServerError, httpx.CodeInternal, msg)
}

func writeOK(w http.ResponseWriter, v any) {
	httpx.JSON(w, http.StatusOK, v)
}
