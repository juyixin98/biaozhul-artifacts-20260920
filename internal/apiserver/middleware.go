package apiserver

import (
	"context"
	"net/http"

	"vfxqueue/internal/auth"
	"vfxqueue/internal/db/gen"

	"github.com/go-chi/chi/v5"
	"github.com/google/uuid"
)

type projKey struct{}

// projectContext validates {projectID}, loads the project, and enforces
// membership: admins access anything; members only their own projects.
func (s *Server) projectContext(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		pid, err := uuid.Parse(chi.URLParam(r, "projectID"))
		if err != nil {
			writeErr(w, http.StatusBadRequest, "invalid project id")
			return
		}
		u := auth.User(r.Context())
		ok, err := auth.CanAccessProject(r.Context(), s.q, u, pid)
		if err != nil {
			writeErr(w, http.StatusInternalServerError, "authorization check failed")
			return
		}
		if !ok {
			writeErr(w, http.StatusForbidden, "not a member of this project")
			return
		}
		proj, err := s.q.GetProject(r.Context(), pid)
		if err != nil {
			writeErr(w, http.StatusNotFound, "project not found")
			return
		}
		ctx := context.WithValue(r.Context(), projKey{}, &proj)
		next.ServeHTTP(w, r.WithContext(ctx))
	})
}

func projectFromCtx(ctx context.Context) *gen.Project {
	p, _ := ctx.Value(projKey{}).(*gen.Project)
	return p
}
