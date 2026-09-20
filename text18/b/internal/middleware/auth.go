package middleware

import (
	"context"
	"net/http"

	"sircc/internal/db"
	"sircc/internal/httpx"
)

type ctxKey string

const actorKey ctxKey = "actor"

// Actor is the authenticated caller. There is no external identity provider:
// the caller presents X-User-Id and it must match a seeded user row.
type Actor struct {
	ID   string
	Role string
	Name string
}

func FromContext(ctx context.Context) (Actor, bool) {
	a, ok := ctx.Value(actorKey).(Actor)
	return a, ok
}

// Authenticate resolves X-User-Id against the users table.
func Authenticate(q db.Querier) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			uid := r.Header.Get("X-User-Id")
			if uid == "" {
				httpx.ErrorJSON(w, http.StatusUnauthorized, httpx.CodeUnauthorized, "missing X-User-Id header")
				return
			}
			user, err := q.GetUser(r.Context(), uid)
			if err != nil {
				httpx.ErrorJSON(w, http.StatusUnauthorized, httpx.CodeUnauthorized, "unknown user")
				return
			}
			ctx := context.WithValue(r.Context(), actorKey, Actor{
				ID: user.ID, Role: user.Role, Name: user.Name,
			})
			next.ServeHTTP(w, r.WithContext(ctx))
		})
	}
}

// RequestID extracts the client-supplied idempotency key.
func RequestID(r *http.Request) string {
	return r.Header.Get("X-Request-Id")
}
