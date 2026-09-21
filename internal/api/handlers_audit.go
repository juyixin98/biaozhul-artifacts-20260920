package api

import (
	"encoding/json"
	"net/http"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgtype"

	"github.com/clearsettle/clearsettle/internal/auth"
	"github.com/clearsettle/clearsettle/internal/store"
)

func (d Deps) handleListAudit(w http.ResponseWriter, r *http.Request) {
	p := principalFrom(r.Context())
	limit, offset := pagination(r)

	// Operators (if ever routed here) are confined to their own merchant;
	// admin and auditor see everything, optionally filtered by ?merchant_id=.
	var merchantFilter *uuid.UUID
	if !p.IsAdmin() && !p.IsAuditor() {
		merchantFilter = p.MerchantID
	} else if raw := r.URL.Query().Get("merchant_id"); raw != "" {
		id, err := uuid.Parse(raw)
		if err != nil {
			writeError(w, http.StatusBadRequest, "bad_merchant_id", "merchant_id must be a UUID")
			return
		}
		merchantFilter = &id
	}
	actionFilter := r.URL.Query().Get("action")

	var rows []store.AuditLog
	var err error
	if p.Role == auth.RoleOperator {
		rows, err = d.Q.ListAuditLogsForMerchant(r.Context(), store.ListAuditLogsForMerchantParams{
			MerchantID: p.MerchantID, RowLimit: limit, RowOffset: offset,
		})
	} else {
		rows, err = d.Q.ListAuditLogs(r.Context(), store.ListAuditLogsParams{
			MerchantID:   merchantFilter,
			ActionFilter: pgtype.Text{String: actionFilter, Valid: actionFilter != ""},
			RowLimit:     limit,
			RowOffset:    offset,
		})
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}

	out := make([]map[string]any, 0, len(rows))
	for _, l := range rows {
		detail := json.RawMessage(l.Detail)
		if len(l.Detail) == 0 {
			detail = json.RawMessage(`{}`)
		}
		row := map[string]any{
			"id": l.ID, "actor_role": l.ActorRole, "action": l.Action,
			"masked": l.Masked, "detail": detail,
			"created_at": l.CreatedAt.Time.Format("2006-01-02T15:04:05Z07:00"),
		}
		if l.ActorUser != nil {
			row["actor_user"] = l.ActorUser.String()
		}
		if l.MerchantID != nil {
			row["merchant_id"] = l.MerchantID.String()
		}
		if l.TargetType.Valid {
			row["target_type"] = l.TargetType.String
		}
		if l.TargetID.Valid {
			row["target_id"] = l.TargetID.String
		}
		if l.Ip.Valid {
			row["ip"] = l.Ip.String
		}
		out = append(out, row)
	}
	writeJSON(w, http.StatusOK, map[string]any{"audit_logs": out, "limit": limit, "offset": offset})
}
