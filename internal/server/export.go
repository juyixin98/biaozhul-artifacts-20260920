package server

import (
	"encoding/csv"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"time"

	"github.com/jackc/pgx/v5"

	"dams.local/dams/internal/auditchain"
	sqlcgen "dams.local/dams/internal/db/sqlc"
)

const exportPageSize = 1000

// maskValue is the replacement for masked sensitive event fields.
const maskValue = "***MASKED***"

type exportPolicyRequest struct {
	MaskedFields []string `json:"masked_fields"`
}

// maskableFields are the event fields an export policy may mask.
var maskableFields = map[string]bool{
	"db_user": true, "table_name": true, "schema_name": true,
	"event_id": true, "row_count": true, "source_key": true,
}

func (s *Server) handleUpdateExportPolicy(w http.ResponseWriter, r *http.Request) {
	org := currentOrg(r)
	p := principal(r)
	var req exportPolicyRequest
	if err := decodeJSON(w, r, &req, 1<<20); err != nil {
		writeError(w, http.StatusBadRequest, "bad_json", "invalid JSON body: "+err.Error(), nil)
		return
	}
	fields := make([]string, 0, len(req.MaskedFields))
	seen := map[string]bool{}
	for _, f := range req.MaskedFields {
		if !maskableFields[f] {
			writeError(w, http.StatusUnprocessableEntity, "bad_field",
				fmt.Sprintf("field %q cannot be masked; allowed: db_user, table_name, schema_name, event_id, row_count, source_key", f), nil)
			return
		}
		if !seen[f] {
			seen[f] = true
			fields = append(fields, f)
		}
	}
	pol, err := s.q.UpsertExportPolicy(r.Context(), sqlcgen.UpsertExportPolicyParams{
		OrgID: org.ID, MaskedFields: fields,
	})
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal_error", err.Error(), nil)
		return
	}
	if err := s.appendAudit(r, org.ID, p.UserID, "export.policy_update", map[string]any{
		"masked_fields": fields,
	}); err != nil {
		writeError(w, http.StatusInternalServerError, "internal_error", err.Error(), nil)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"org_id": pol.OrgID, "masked_fields": pol.MaskedFields,
	})
}

// handleExport streams events for a time range. All organization members may
// export (auditors read-only, analysts for investigation, admins unrestricted)
// but sensitive fields configured in the export policy are always masked;
// there is no way to bypass masking via the API.
//
// Formats: format=jsonl (default, streamed JSON Lines), format=csv.
func (s *Server) handleExport(w http.ResponseWriter, r *http.Request) {
	org := currentOrg(r)
	p := principal(r)
	from, to, ok2 := parseTimeRange(w, r)
	if !ok2 {
		return
	}
	format := r.URL.Query().Get("format")
	if format == "" {
		format = "jsonl"
	}
	if format != "jsonl" && format != "csv" {
		writeError(w, http.StatusBadRequest, "bad_format", "format must be jsonl or csv", nil)
		return
	}

	policy, err := s.q.GetExportPolicy(r.Context(), org.ID)
	if errors.Is(err, pgx.ErrNoRows) {
		policy.MaskedFields = []string{"db_user", "table_name"}
	} else if err != nil {
		writeError(w, http.StatusInternalServerError, "internal_error", err.Error(), nil)
		return
	}
	mask := map[string]bool{}
	for _, f := range policy.MaskedFields {
		mask[f] = true
	}

	if err := s.appendAudit(r, org.ID, p.UserID, "export.events", map[string]any{
		"format": format,
		"from":   rfcOrNil(from), "to": rfcOrNil(to),
		"masked_fields": policy.MaskedFields,
	}); err != nil {
		writeError(w, http.StatusInternalServerError, "internal_error", err.Error(), nil)
		return
	}

	var cursorTime pgtypeTimestamptz
	cursorID := int64(0)
	first := true

	switch format {
	case "csv":
		w.Header().Set("Content-Type", "text/csv; charset=utf-8")
		w.Header().Set("Content-Disposition",
			`attachment; filename="dams-events-`+org.Slug+`.csv"`)
		cw := csv.NewWriter(w)
		_ = cw.Write([]string{"id", "source", "event_id", "db_user",
			"occurred_at", "action", "schema", "table", "row_count"})
		cw.Flush()
		for {
			page, gErr := s.q.ExportEventsPage(r.Context(), sqlcgen.ExportEventsPageParams{
				OrgID: org.ID, Column2: from, Column3: to,
				Column4: cursorID, Column5: cursorTime, Column6: cursorID,
				Limit: exportPageSize,
			})
			if gErr != nil {
				return
			}
			if len(page) == 0 {
				break
			}
			for _, e := range page {
				_ = cw.Write([]string{
					strconv.FormatInt(e.ID, 10),
					maskedStr(mask, "source_key", e.SourceKey),
					maskedStr(mask, "event_id", e.EventID),
					maskedStr(mask, "db_user", e.DbUser),
					e.OccurredAt.Time.UTC().Format(time.RFC3339Nano),
					e.Action,
					maskedStr(mask, "schema_name", e.SchemaName),
					maskedStr(mask, "table_name", e.TableName),
					maskedInt(mask, "row_count", e.RowCount),
				})
			}
			cw.Flush()
			last := page[len(page)-1]
			cursorTime = last.OccurredAt
			cursorID = last.ID
			first = false
			if len(page) < exportPageSize {
				break
			}
		}
	default: // jsonl
		w.Header().Set("Content-Type", "application/x-ndjson; charset=utf-8")
		w.Header().Set("Content-Disposition",
			`attachment; filename="dams-events-`+org.Slug+`.ndjson"`)
		flusher, _ := w.(http.Flusher)
		enc := json.NewEncoder(w)
		_ = enc.Encode(map[string]any{
			"export_header": "dams-events/v1", "org": org.Slug,
			"masked_fields": policy.MaskedFields,
			"generated_at":  time.Now().UTC().Format(time.RFC3339Nano),
		})
		for {
			page, gErr := s.q.ExportEventsPage(r.Context(), sqlcgen.ExportEventsPageParams{
				OrgID: org.ID, Column2: from, Column3: to,
				Column4: cursorID, Column5: cursorTime, Column6: cursorID,
				Limit: exportPageSize,
			})
			if gErr != nil {
				return
			}
			if len(page) == 0 {
				break
			}
			for _, e := range page {
				_ = enc.Encode(eventExportRow(e, mask))
			}
			if flusher != nil {
				flusher.Flush()
			}
			last := page[len(page)-1]
			cursorTime = last.OccurredAt
			cursorID = last.ID
			first = false
			if len(page) < exportPageSize {
				break
			}
		}
	}
	_ = first
}

func eventExportRow(e sqlcgen.Event, mask map[string]bool) map[string]any {
	return map[string]any{
		"id":          e.ID,
		"source":      maskedStr(mask, "source_key", e.SourceKey),
		"event_id":    maskedStr(mask, "event_id", e.EventID),
		"db_user":     maskedStr(mask, "db_user", e.DbUser),
		"occurred_at": e.OccurredAt.Time.UTC().Format(time.RFC3339Nano),
		"action":      e.Action,
		"schema":      maskedStr(mask, "schema_name", e.SchemaName),
		"table":       maskedStr(mask, "table_name", e.TableName),
		"row_count":   maskedInt(mask, "row_count", e.RowCount),
		"received_at": e.ReceivedAt.Time.UTC().Format(time.RFC3339Nano),
	}
}

func maskedStr(mask map[string]bool, field, value string) string {
	if mask[field] {
		return maskValue
	}
	return value
}

func maskedInt(mask map[string]bool, field string, value int64) string {
	if mask[field] {
		return maskValue
	}
	return strconv.FormatInt(value, 10)
}

func parseTimeRange(w http.ResponseWriter, r *http.Request) (pgtypeTimestamptz, pgtypeTimestamptz, bool) {
	var from, to pgtypeTimestamptz
	if v := r.URL.Query().Get("from"); v != "" {
		t, err := time.Parse(time.RFC3339, v)
		if err != nil {
			writeError(w, http.StatusBadRequest, "bad_time", "from must be RFC3339", nil)
			return from, to, false
		}
		from = pgtypeTimestamptz{Time: t.UTC(), Valid: true}
	}
	if v := r.URL.Query().Get("to"); v != "" {
		t, err := time.Parse(time.RFC3339, v)
		if err != nil {
			writeError(w, http.StatusBadRequest, "bad_time", "to must be RFC3339", nil)
			return from, to, false
		}
		to = pgtypeTimestamptz{Time: t.UTC(), Valid: true}
	}
	if from.Valid && to.Valid && !from.Time.Before(to.Time) {
		writeError(w, http.StatusBadRequest, "bad_range", "require from < to", nil)
		return from, to, false
	}
	return from, to, true
}

func rfcOrNil(t pgtypeTimestamptz) any {
	if !t.Valid {
		return nil
	}
	return t.Time.UTC().Format(time.RFC3339Nano)
}

// appendAudit writes one entry in its own short transaction (used by
// lightweight endpoints that don't already hold one).
func (s *Server) appendAudit(r *http.Request, orgID, actorID int64, action string, content any) error {
	return runTx(r.Context(), s.pool, func(q *sqlcgen.Queries, tx pgx.Tx) error {
		_, _, err := auditchain.Append(r.Context(), tx, q, orgID, actorID, action, content)
		return err
	})
}
