package api

import (
	"encoding/json"

	"dams/internal/db"
)

// jsonb renders raw JSONB bytes as an inline JSON value instead of the
// base64 string Go would produce for []byte.
func jsonb(raw []byte) any {
	if len(raw) == 0 {
		return map[string]any{}
	}
	var v any
	if err := json.Unmarshal(raw, &v); err != nil {
		return string(raw)
	}
	return v
}

func presentRule(rv db.RuleVersion) map[string]any {
	return map[string]any{
		"id":           rv.ID,
		"org_id":       rv.OrgID,
		"rule_type":    rv.RuleType,
		"version":      rv.Version,
		"is_active":    rv.IsActive,
		"params":       jsonb(rv.Params),
		"effective_at": rv.EffectiveAt.Time,
		"created_at":   rv.CreatedAt.Time,
	}
}

func presentRules(rows []db.RuleVersion) []map[string]any {
	out := make([]map[string]any, 0, len(rows))
	for _, r := range rows {
		out = append(out, presentRule(r))
	}
	return out
}

func presentAlert(a db.Alert) map[string]any {
	return map[string]any{
		"id":              a.ID,
		"org_id":          a.OrgID,
		"rule_version_id": a.RuleVersionID,
		"rule_type":       a.RuleType,
		"fingerprint":     a.Fingerprint,
		"status":          a.Status,
		"version":         a.Version,
		"title":           a.Title,
		"detail":          jsonb(a.Detail),
		"created_at":      a.CreatedAt.Time,
		"updated_at":      a.UpdatedAt.Time,
	}
}

func presentAlerts(rows []db.Alert) []map[string]any {
	out := make([]map[string]any, 0, len(rows))
	for _, a := range rows {
		out = append(out, presentAlert(a))
	}
	return out
}

func presentHistory(rows []db.AlertStatusHistory) []map[string]any {
	out := make([]map[string]any, 0, len(rows))
	for _, h := range rows {
		from := ""
		if h.FromStatus.Valid {
			from = h.FromStatus.String
		}
		out = append(out, map[string]any{
			"id":          h.ID,
			"from_status": from,
			"to_status":   h.ToStatus,
			"note":        h.Note,
			"created_at":  h.CreatedAt.Time,
		})
	}
	return out
}

func presentEvidence(rows []db.AlertEvidenceRevision) []map[string]any {
	out := make([]map[string]any, 0, len(rows))
	for _, e := range rows {
		out = append(out, map[string]any{
			"id":          e.ID,
			"seq":         e.Seq,
			"event_count": e.EventCount,
			"detail":      jsonb(e.Detail),
			"created_at":  e.CreatedAt.Time,
		})
	}
	return out
}

func presentAuditEntry(e db.AuditEntry) map[string]any {
	return map[string]any{
		"id":          e.ID,
		"seq":         e.Seq,
		"entry_type":  e.EntryType,
		"actor_label": e.ActorLabel,
		"payload":     jsonb(e.Payload),
		"prev_hash":   e.PrevHash,
		"entry_hash":  e.EntryHash,
		"created_at":  e.CreatedAt.Time,
	}
}

func presentAuditEntries(rows []db.AuditEntry) []map[string]any {
	out := make([]map[string]any, 0, len(rows))
	for _, e := range rows {
		out = append(out, presentAuditEntry(e))
	}
	return out
}
