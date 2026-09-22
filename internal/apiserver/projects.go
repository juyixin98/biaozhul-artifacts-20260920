package apiserver

import (
	"context"
	"net/http"

	"vfxqueue/internal/auth"
	"vfxqueue/internal/db/gen"
	"vfxqueue/internal/idgen"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

func (s *Server) getMe(w http.ResponseWriter, r *http.Request) {
	u := auth.User(r.Context())
	writeJSON(w, 200, map[string]any{
		"id": u.ID, "email": u.Email, "display_name": u.DisplayName, "role": u.Role,
	})
}

type createProjectReq struct {
	Name string `json:"name"`
}

func (s *Server) createProject(w http.ResponseWriter, r *http.Request) {
	u := auth.User(r.Context())
	var req createProjectReq
	if err := decodeJSON(r, &req); err != nil || req.Name == "" {
		writeErr(w, http.StatusBadRequest, "name required")
		return
	}
	tx, err := s.pool.BeginTx(r.Context(), pgx.TxOptions{})
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	defer tx.Rollback(r.Context())
	qtx := s.q.WithTx(tx)
	proj, err := qtx.CreateProject(r.Context(), gen.CreateProjectParams{
		ID: idgen.NewID(), Name: req.Name, CreatedBy: u.ID,
	})
	if err != nil {
		writeErr(w, 400, err.Error())
		return
	}
	if err := qtx.AddProjectMember(r.Context(), gen.AddProjectMemberParams{
		ProjectID: proj.ID, UserID: u.ID, Role: "owner",
	}); err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	if err := tx.Commit(r.Context()); err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	writeJSON(w, 201, projectView(proj, "owner"))
}

func (s *Server) listProjects(w http.ResponseWriter, r *http.Request) {
	u := auth.User(r.Context())
	if u.Role == "admin" {
		// Admins see all projects via the membership-independent listing.
		rows, err := s.q.ListAllProjects(r.Context())
		if err != nil {
			writeErr(w, 500, err.Error())
			return
		}
		writeJSON(w, 200, rows)
		return
	}
	rows, err := s.q.ListMemberships(r.Context(), u.ID)
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	type item struct {
		ProjectID uuid.UUID `json:"project_id"`
		Role      string    `json:"role"`
	}
	out := make([]item, 0, len(rows))
	for _, m := range rows {
		out = append(out, item{ProjectID: m.ProjectID, Role: m.Role})
	}
	writeJSON(w, 200, out)
}

func (s *Server) getProject(w http.ResponseWriter, r *http.Request) {
	proj := projectFromCtx(r.Context())
	role := "member"
	if u := auth.User(r.Context()); u.Role == "admin" {
		role = "admin"
	} else if m, err := s.q.GetProjectMembership(r.Context(), gen.GetProjectMembershipParams{
		ProjectID: proj.ID, UserID: auth.User(r.Context()).ID,
	}); err == nil {
		role = m.Role
	}
	writeJSON(w, 200, projectView(*proj, role))
}

type addMemberReq struct {
	UserID uuid.UUID `json:"user_id"`
	Role   string    `json:"role"`
}

func (s *Server) addMember(w http.ResponseWriter, r *http.Request) {
	proj := projectFromCtx(r.Context())
	caller := auth.User(r.Context())
	// Only project owner (or global admin) may add members.
	if caller.Role != "admin" {
		m, err := s.q.GetProjectMembership(r.Context(), gen.GetProjectMembershipParams{
			ProjectID: proj.ID, UserID: caller.ID,
		})
		if err != nil || m.Role != "owner" {
			writeErr(w, http.StatusForbidden, "only project owner can add members")
			return
		}
	}
	var req addMemberReq
	if err := decodeJSON(r, &req); err != nil {
		writeErr(w, 400, "invalid body")
		return
	}
	if req.Role == "" {
		req.Role = "member"
	}
	if req.Role != "owner" && req.Role != "member" {
		writeErr(w, 400, "role must be owner|member")
		return
	}
	if _, err := s.q.GetUserByID(r.Context(), req.UserID); err != nil {
		writeErr(w, 404, "user not found")
		return
	}
	if err := s.q.AddProjectMember(r.Context(), gen.AddProjectMemberParams{
		ProjectID: proj.ID, UserID: req.UserID, Role: req.Role,
	}); err != nil {
		writeErr(w, 400, err.Error())
		return
	}
	w.WriteHeader(204)
}

func (s *Server) listMembers(w http.ResponseWriter, r *http.Request) {
	proj := projectFromCtx(r.Context())
	rows, err := s.q.ListProjectMembers(r.Context(), proj.ID)
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	writeJSON(w, 200, rows)
}

func projectView(p gen.Project, role string) map[string]any {
	return map[string]any{
		"id": p.ID, "name": p.Name, "created_by": p.CreatedBy,
		"created_at": p.CreatedAt, "my_role": role,
	}
}

var _ = context.Background
