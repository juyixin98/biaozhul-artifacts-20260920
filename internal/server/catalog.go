package server

import "net/http"

// handleListOrgs returns only the organizations the caller belongs to.
func (s *Server) handleListOrgs(w http.ResponseWriter, r *http.Request) {
	p := principal(r)
	all, err := s.q.ListOrgs(r.Context())
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal_error", err.Error(), nil)
		return
	}
	out := make([]map[string]any, 0)
	for _, o := range all {
		role, member := p.RoleIn(o.ID)
		if !member {
			continue
		}
		out = append(out, map[string]any{
			"id": o.ID, "slug": o.Slug, "name": o.Name,
			"timezone": o.Timezone, "role": string(role),
		})
	}
	writeJSON(w, http.StatusOK, map[string]any{"organizations": out})
}

func (s *Server) handleListSources(w http.ResponseWriter, r *http.Request) {
	org := currentOrg(r)
	sources, err := s.q.ListSources(r.Context(), org.ID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal_error", err.Error(), nil)
		return
	}
	out := make([]map[string]any, 0, len(sources))
	for _, src := range sources {
		out = append(out, map[string]any{
			"id": src.ID, "source_key": src.SourceKey, "name": src.Name,
			"created_at": src.CreatedAt.Time,
		})
	}
	writeJSON(w, http.StatusOK, map[string]any{"sources": out})
}
