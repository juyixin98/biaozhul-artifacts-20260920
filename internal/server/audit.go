package server

import (
	"encoding/json"
	"errors"
	"net/http"
	"strconv"

	"github.com/jackc/pgx/v5"

	"dams.local/dams/internal/auditchain"
	"dams.local/dams/internal/auth"
	sqlcgen "dams.local/dams/internal/db/sqlc"
)

func (s *Server) handleListAudit(w http.ResponseWriter, r *http.Request) {
	org := currentOrg(r)
	p := principal(r)
	if !p.HasRole(org.ID, auth.RoleAdmin, auth.RoleAuditor) {
		writeError(w, http.StatusForbidden, "forbidden", "audit log requires admin or auditor", nil)
		return
	}
	after := int64(0)
	limit := int32(500)
	if v := r.URL.Query().Get("after_seq"); v != "" {
		if n, err := strconv.ParseInt(v, 10, 64); err == nil && n >= 0 {
			after = n
		}
	}
	if v := r.URL.Query().Get("limit"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 && n <= 1000 {
			limit = int32(n)
		}
	}
	entries, err := s.q.ListAuditEntries(r.Context(), sqlcgen.ListAuditEntriesParams{
		OrgID: org.ID, Seq: after, Limit: limit,
	})
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal_error", err.Error(), nil)
		return
	}
	counts, err := s.q.CountAuditEntries(r.Context(), org.ID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal_error", err.Error(), nil)
		return
	}
	out := make([]map[string]any, 0, len(entries))
	for _, e := range entries {
		out = append(out, auditDTO(e))
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"entries": out, "count": counts.Cnt, "max_seq": counts.MaxSeq,
	})
}

// handleVerifyAudit recomputes every hash of the organization's chain and
// reports gaps (missing entries), broken links (rewrite/out-of-order
// reinsertion), tampered content and invalid entry hashes.
func (s *Server) handleVerifyAudit(w http.ResponseWriter, r *http.Request) {
	org := currentOrg(r)
	p := principal(r)
	if !p.HasRole(org.ID, auth.RoleAdmin, auth.RoleAuditor) {
		writeError(w, http.StatusForbidden, "forbidden", "audit verification requires admin or auditor", nil)
		return
	}
	rep, err := auditchain.Verify(r.Context(), s.q, org.ID)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			writeJSON(w, http.StatusOK, map[string]any{"healthy": true, "issues": []any{}})
			return
		}
		writeError(w, http.StatusInternalServerError, "internal_error", err.Error(), nil)
		return
	}
	status := http.StatusOK
	if !rep.Healthy {
		status = http.StatusConflict
	}
	writeJSON(w, status, rep)
}

func auditDTO(e sqlcgen.AuditEntry) map[string]any {
	var content json.RawMessage = e.Content
	return map[string]any{
		"seq": e.Seq, "org_id": e.OrgID,
		"actor_id": e.ActorID, "action": e.Action,
		"content":      content,
		"content_hash": e.ContentHash, "prev_hash": e.PrevHash,
		"entry_hash": e.EntryHash,
		"created_at": e.CreatedAt.Time.UTC().Format("2006-01-02T15:04:05.999999999Z07:00"),
	}
}
