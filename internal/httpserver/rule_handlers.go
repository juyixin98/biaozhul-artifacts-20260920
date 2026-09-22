package httpserver

import (
	"net/http"

	"communityvault/internal/db"
)

type ruleSummaryView struct {
	ID      int64  `json:"version_id"`
	Version int32  `json:"version"`
	Active  bool   `json:"active"`
	Note    string `json:"note"`
}

func ruleSummary(rv db.RuleVersion) ruleSummaryView {
	return ruleSummaryView{ID: rv.ID, Version: rv.Version, Active: rv.Active, Note: rv.Note}
}

func (s *Server) handleListRules(w http.ResponseWriter, r *http.Request) {
	if _, err := requireRole(r, "moderator", "admin"); err != nil {
		writeErr(w, err)
		return
	}
	rvs, err := s.rules.ListVersions(r.Context())
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, 200, map[string]any{"rules": rvs})
}

func (s *Server) handleActiveRule(w http.ResponseWriter, r *http.Request) {
	if _, err := requireRole(r, "moderator", "admin"); err != nil {
		writeErr(w, err)
		return
	}
	rv, words, err := s.rules.Active(r.Context())
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, 200, map[string]any{"rule": rv, "words": words})
}

type createRuleReq struct {
	Note  string   `json:"note"`
	Words []string `json:"words"`
}

func (s *Server) handleCreateRule(w http.ResponseWriter, r *http.Request) {
	if _, err := requireRole(r, "admin"); err != nil {
		writeErr(w, err)
		return
	}
	var req createRuleReq
	if err := decode(r, &req); err != nil {
		writeErr(w, err)
		return
	}
	rv, err := s.rules.CreateVersion(r.Context(), req.Note, req.Words)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, 201, map[string]any{"rule": rv})
}

func (s *Server) handleActivateRule(w http.ResponseWriter, r *http.Request) {
	if _, err := requireRole(r, "admin"); err != nil {
		writeErr(w, err)
		return
	}
	id, err := idParam(r, "id")
	if err != nil {
		writeErr(w, err)
		return
	}
	rv, err := s.rules.Activate(r.Context(), id)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, 200, map[string]any{"rule": rv})
}
