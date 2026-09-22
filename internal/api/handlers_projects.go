package api

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"

	"github.com/go-chi/chi/v5"
	"github.com/google/uuid"

	"github.com/vfxqueue/renderq/internal/db/dbgen"
)

// createProjectRequest only needs a name; the creator becomes the owner and
// (via trigger) a member.
type createProjectRequest struct {
	Name string `json:"name"`
}

func (s *Server) createProject(w http.ResponseWriter, r *http.Request) {
	u := currentUser(r)
	var req createProjectRequest
	if err := decodeJSON(r, &req); err != nil || req.Name == "" {
		writeError(w, http.StatusBadRequest, `invalid body: {"name": string} required`)
		return
	}
	p, err := s.q.CreateProject(r.Context(), dbgen.CreateProjectParams{
		Name: req.Name, OwnerID: u.ID,
	})
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusCreated, projectDTOFrom(p))
}

func (s *Server) listProjects(w http.ResponseWriter, r *http.Request) {
	u := currentUser(r)
	if isAdmin(u) {
		// Admins see all projects; reuse the membership listing would hide
		// projects they don't belong to, so query via members of anyone is
		// unsuitable — list through a dedicated path.
		ps, err := s.q.ListAllProjects(r.Context())
		if err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}
		out := make([]projectDTO, 0, len(ps))
		for _, p := range ps {
			out = append(out, projectDTOFrom(p))
		}
		writeJSON(w, http.StatusOK, out)
		return
	}
	ps, err := s.q.ListProjectsForUser(r.Context(), u.ID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	out := make([]projectDTO, 0, len(ps))
	for _, p := range ps {
		out = append(out, projectDTOFrom(p))
	}
	writeJSON(w, http.StatusOK, out)
}

func (s *Server) getProject(w http.ResponseWriter, r *http.Request) {
	pid, ok := parseUUID(w, r, "projectID")
	if !ok {
		return
	}
	if !s.authorizeProject(w, r, pid) {
		return
	}
	p, err := s.q.GetProject(r.Context(), pid)
	if err != nil {
		writeError(w, http.StatusNotFound, "project not found")
		return
	}
	writeJSON(w, http.StatusOK, projectDTOFrom(p))
}

type addMemberRequest struct {
	Username string `json:"username"`
}

func (s *Server) addProjectMember(w http.ResponseWriter, r *http.Request) {
	pid, ok := parseUUID(w, r, "projectID")
	if !ok {
		return
	}
	// Only the owner or an admin may add members.
	p, err := s.q.GetProject(r.Context(), pid)
	if err != nil {
		writeError(w, http.StatusNotFound, "project not found")
		return
	}
	u := currentUser(r)
	if !isAdmin(u) && p.OwnerID != u.ID {
		writeError(w, http.StatusForbidden, "only the project owner or an admin may add members")
		return
	}
	var req addMemberRequest
	if err := decodeJSON(r, &req); err != nil || req.Username == "" {
		writeError(w, http.StatusBadRequest, `invalid body: {"username": string} required`)
		return
	}
	target, err := s.q.GetUserByName(r.Context(), req.Username)
	if err != nil {
		writeError(w, http.StatusNotFound, "user not found")
		return
	}
	if err := s.q.AddProjectMember(r.Context(), dbgen.AddProjectMemberParams{
		ProjectID: pid, UserID: target.ID,
	}); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusNoContent, nil)
}

func (s *Server) listProjectMembers(w http.ResponseWriter, r *http.Request) {
	pid, ok := parseUUID(w, r, "projectID")
	if !ok {
		return
	}
	if !s.authorizeProject(w, r, pid) {
		return
	}
	us, err := s.q.ListProjectMembers(r.Context(), pid)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	out := make([]userDTO, 0, len(us))
	for _, m := range us {
		out = append(out, userDTOFrom(m))
	}
	writeJSON(w, http.StatusOK, out)
}

func parseUUID(w http.ResponseWriter, r *http.Request, param string) (uuid.UUID, bool) {
	raw := chi.URLParam(r, param)
	id, err := uuid.Parse(raw)
	if err != nil {
		writeError(w, http.StatusBadRequest, fmt.Sprintf("invalid %s", param))
		return uuid.Nil, false
	}
	return id, true
}

var _ = errors.New

func manifestDigest(canonical []byte) string {
	sum := sha256.Sum256(canonical)
	return hex.EncodeToString(sum[:])
}

func mustJSON(v any) []byte {
	b, _ := json.Marshal(v)
	return b
}
