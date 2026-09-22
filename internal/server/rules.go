package server

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"strings"

	"github.com/go-chi/chi/v5"
	"github.com/jackc/pgx/v5"

	"dams.local/dams/internal/auditchain"
	sqlcgen "dams.local/dams/internal/db/sqlc"
)

// errHandled means the handler already wrote the HTTP response (e.g. a 404
// discovered inside the transaction); the outer wrapper must not write again.
var errHandled = errors.New("request handled")

type ruleRequest struct {
	Kind          string   `json:"kind"`
	Name          string   `json:"name"`
	Enabled       *bool    `json:"enabled"`
	WindowSeconds *int32   `json:"window_seconds"`
	MaxEvents     *int32   `json:"max_events"`
	Tables        []string `json:"tables"`
	HourStart     *int32   `json:"hour_start"`
	HourEnd       *int32   `json:"hour_end"`
	Config        any      `json:"config"`
	// Update an existing rule's definition (creates a new version).
	// When omitted, a new rule id is created at version 1.
	RuleID *int64 `json:"rule_id"`
}

func (s *Server) handleCreateRule(w http.ResponseWriter, r *http.Request) {
	org := currentOrg(r)
	p := principal(r)
	var req ruleRequest
	if err := decodeJSON(w, r, &req, 1<<20); err != nil {
		writeError(w, http.StatusBadRequest, "bad_json", "invalid JSON body: "+err.Error(), nil)
		return
	}
	if req.Name == "" {
		writeError(w, http.StatusUnprocessableEntity, "invalid_rule", "name is required", nil)
		return
	}

	enabled := true
	if req.Enabled != nil {
		enabled = *req.Enabled
	}
	windowSeconds := int32(300)
	maxEvents := int32(500)
	var tables []string
	hourStart := int32(6)
	hourEnd := int32(20)

	switch req.Kind {
	case "rate":
		if req.WindowSeconds != nil {
			windowSeconds = *req.WindowSeconds
		}
		if req.MaxEvents != nil {
			maxEvents = *req.MaxEvents
		}
		if windowSeconds < 1 || windowSeconds > 86400 {
			writeError(w, http.StatusUnprocessableEntity, "invalid_rule",
				"window_seconds must be between 1 and 86400", nil)
			return
		}
		if maxEvents < 1 {
			writeError(w, http.StatusUnprocessableEntity, "invalid_rule",
				"max_events must be >= 1", nil)
			return
		}
	case "sensitive":
		if req.HourStart != nil {
			hourStart = *req.HourStart
		}
		if req.HourEnd != nil {
			hourEnd = *req.HourEnd
		}
		tables = normalizeTables(req.Tables)
		if len(tables) == 0 {
			writeError(w, http.StatusUnprocessableEntity, "invalid_rule",
				"tables must contain at least one entry", nil)
			return
		}
		if hourStart < 0 || hourStart > 23 || hourEnd < 1 || hourEnd > 24 {
			writeError(w, http.StatusUnprocessableEntity, "invalid_rule",
				"hours must satisfy 0 <= hour_start <= 23 and 1 <= hour_end <= 24", nil)
			return
		}
		if hourStart >= hourEnd {
			writeError(w, http.StatusUnprocessableEntity, "invalid_rule",
				"only non-wrapping windows are supported: require hour_start < hour_end", nil)
			return
		}
	default:
		writeError(w, http.StatusUnprocessableEntity, "invalid_rule",
			"kind must be 'rate' or 'sensitive'", nil)
		return
	}

	configJSON, err := json.Marshal(orEmptyObject(req.Config))
	if err != nil {
		writeError(w, http.StatusBadRequest, "bad_json", "invalid config: "+err.Error(), nil)
		return
	}
	// Rate rules carry no table list; insert an empty array rather than NULL
	// (the column is NOT NULL).
	if tables == nil {
		tables = []string{}
	}

	var created sqlcgen.Rule
	err = runTx(r.Context(), s.pool, func(q *sqlcgen.Queries, tx pgx.Tx) error {
		var ruleID int64
		var version int32
		if req.RuleID != nil {
			versions, gErr := q.ListRuleVersions(r.Context(),
				sqlcgen.ListRuleVersionsParams{OrgID: org.ID, ID: *req.RuleID})
			if gErr != nil || len(versions) == 0 {
				writeError(w, http.StatusNotFound, "not_found",
					fmt.Sprintf("rule %d not found in this organization", *req.RuleID), nil)
				return errHandled
			}
			ruleID = *req.RuleID
			version, gErr = q.NextRuleVersion(r.Context(),
				sqlcgen.NextRuleVersionParams{OrgID: org.ID, ID: ruleID})
			if gErr != nil {
				return gErr
			}
		} else {
			newID, gErr := q.AllocateRuleID(r.Context())
			if gErr != nil {
				return gErr
			}
			ruleID, version = newID, 1
		}

		var insErr error
		created, insErr = q.InsertRule(r.Context(), sqlcgen.InsertRuleParams{
			ID:            ruleID,
			OrgID:         org.ID,
			Version:       version,
			Kind:          req.Kind,
			Name:          req.Name,
			Enabled:       enabled,
			WindowSeconds: windowSeconds,
			MaxEvents:     maxEvents,
			Tables:        tables,
			HourStart:     hourStart,
			HourEnd:       hourEnd,
			ConfigJson:    configJSON,
			CreatedBy:     &p.UserID,
		})
		if insErr != nil {
			return insErr
		}

		_, _, aErr := auditchain.Append(r.Context(), tx, q, org.ID, p.UserID,
			"rule.configure", map[string]any{
				"rule_id": ruleID, "version": version, "kind": req.Kind,
				"name": req.Name, "enabled": enabled,
				"window_seconds": windowSeconds, "max_events": maxEvents,
				"tables": tables, "hour_start": hourStart, "hour_end": hourEnd,
				"updated": req.RuleID != nil,
			})
		return aErr
	})
	if errors.Is(err, errHandled) {
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, "rule_create_failed", err.Error(), nil)
		return
	}
	writeJSON(w, http.StatusCreated, ruleDTO(created))
}

func normalizeTables(in []string) []string {
	out := make([]string, 0, len(in))
	seen := map[string]bool{}
	for _, t := range in {
		t = strings.ToLower(strings.TrimSpace(t))
		if t == "" || seen[t] {
			continue
		}
		seen[t] = true
		out = append(out, t)
	}
	return out
}

func orEmptyObject(v any) any {
	if v == nil {
		return map[string]any{}
	}
	return v
}

func (s *Server) handleListRules(w http.ResponseWriter, r *http.Request) {
	org := currentOrg(r)
	rules, err := s.q.ListRules(r.Context(), org.ID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal_error", err.Error(), nil)
		return
	}
	out := make([]map[string]any, 0, len(rules))
	for _, rule := range rules {
		out = append(out, ruleDTO(rule))
	}
	writeJSON(w, http.StatusOK, map[string]any{"rules": out})
}

func (s *Server) handleListRuleVersions(w http.ResponseWriter, r *http.Request) {
	org := currentOrg(r)
	id, err := strconv.ParseInt(chi.URLParam(r, "id"), 10, 64)
	if err != nil {
		writeError(w, http.StatusBadRequest, "bad_id", "rule id must be an integer", nil)
		return
	}
	versions, err := s.q.ListRuleVersions(r.Context(),
		sqlcgen.ListRuleVersionsParams{OrgID: org.ID, ID: id})
	if err != nil || len(versions) == 0 {
		writeError(w, http.StatusNotFound, "not_found", "rule not found", nil)
		return
	}
	out := make([]map[string]any, 0, len(versions))
	for _, v := range versions {
		out = append(out, ruleDTO(v))
	}
	writeJSON(w, http.StatusOK, map[string]any{"versions": out})
}

func ruleDTO(r sqlcgen.Rule) map[string]any {
	return map[string]any{
		"id": r.ID, "org_id": r.OrgID, "version": r.Version,
		"kind": r.Kind, "name": r.Name, "enabled": r.Enabled,
		"window_seconds": r.WindowSeconds, "max_events": r.MaxEvents,
		"tables": r.Tables, "hour_start": r.HourStart, "hour_end": r.HourEnd,
		"config":     json.RawMessage(orBytes(r.ConfigJson)),
		"created_at": r.CreatedAt.Time, "created_by": r.CreatedBy,
	}
}

func orBytes(b []byte) []byte {
	if len(b) == 0 {
		return []byte("{}")
	}
	return b
}
