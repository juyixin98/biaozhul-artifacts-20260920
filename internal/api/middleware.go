package api

import (
	"context"
	"net/http"
	"strings"

	"github.com/google/uuid"

	"github.com/vfxqueue/renderq/internal/db/dbgen"
)

type ctxKey int

const (
	ctxUser ctxKey = iota
)

// auth authenticates the API key and loads the user. Keys arrive via
// X-API-Key or "Authorization: Bearer <key>". Only the key's sha256 digest
// is stored, so a database disclosure does not reveal usable keys.
func (s *Server) auth(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		key := r.Header.Get("X-API-Key")
		if key == "" {
			if a := r.Header.Get("Authorization"); strings.HasPrefix(a, "Bearer ") {
				key = strings.TrimPrefix(a, "Bearer ")
			}
		}
		if key == "" {
			writeError(w, http.StatusUnauthorized, "missing API key")
			return
		}
		user, err := s.q.GetUserByAPIKeyHash(r.Context(), hashKey(key))
		if err != nil {
			writeError(w, http.StatusUnauthorized, "invalid API key")
			return
		}
		next.ServeHTTP(w, r.WithContext(context.WithValue(r.Context(), ctxUser, user)))
	})
}

func currentUser(r *http.Request) dbgen.User {
	return r.Context().Value(ctxUser).(dbgen.User)
}

func isAdmin(u dbgen.User) bool { return u.Role == "admin" }

// authorizeProject returns 0 and writes a 403/404 when the user may not
// access the project; otherwise it returns the project id. Admins may
// access every project; members only projects they belong to.
func (s *Server) authorizeProject(w http.ResponseWriter, r *http.Request, projectID uuid.UUID) bool {
	u := currentUser(r)
	if isAdmin(u) {
		return true
	}
	ok, err := s.q.IsProjectMember(r.Context(), dbgen.IsProjectMemberParams{
		ProjectID: projectID, UserID: u.ID,
	})
	if err != nil {
		writeError(w, http.StatusInternalServerError, "authorization check failed")
		return false
	}
	if !ok {
		writeError(w, http.StatusForbidden, "not a member of this project")
		return false
	}
	return true
}
