// Package auth provides bearer-token authentication and project-level
// authorization for the HTTP API.
package auth

import (
	"context"
	"net/http"
	"strings"

	"vfxqueue/internal/db/gen"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

type ctxKey int

const userKey ctxKey = iota

// Middleware authenticates Authorization: Bearer <token>.
func Middleware(pool *pgxpool.Pool, q *gen.Queries) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			tok := bearer(r.Header.Get("Authorization"))
			if tok == "" {
				http.Error(w, `{"error":"missing bearer token"}`, http.StatusUnauthorized)
				return
			}
			u, err := q.GetUserByAPIToken(r.Context(), tok)
			if err != nil {
				if err == pgx.ErrNoRows {
					http.Error(w, `{"error":"invalid token"}`, http.StatusUnauthorized)
					return
				}
				http.Error(w, `{"error":"internal error"}`, http.StatusInternalServerError)
				return
			}
			next.ServeHTTP(w, r.WithContext(context.WithValue(r.Context(), userKey, &u)))
		})
	}
}

func bearer(h string) string {
	const p = "Bearer "
	if len(h) > len(p) && strings.EqualFold(h[:len(p)], p) {
		return strings.TrimSpace(h[len(p):])
	}
	return ""
}

// User returns the authenticated user, or nil.
func User(ctx context.Context) *gen.User {
	u, _ := ctx.Value(userKey).(*gen.User)
	return u
}

// CanAccessProject reports whether u may read/write the given project:
// admins always may; members must have a membership row.
func CanAccessProject(ctx context.Context, q *gen.Queries, u *gen.User, projectID uuid.UUID) (bool, error) {
	if u.Role == "admin" {
		return true, nil
	}
	_, err := q.GetProjectMembership(ctx, gen.GetProjectMembershipParams{
		ProjectID: projectID, UserID: u.ID,
	})
	if err == nil {
		return true, nil
	}
	if err == pgx.ErrNoRows {
		return false, nil
	}
	return false, err
}
