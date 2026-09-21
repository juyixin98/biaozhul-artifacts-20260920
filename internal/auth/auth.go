// Package auth implements the deliberately simple development authentication:
// the caller presents "X-User-ID: <id>". There are no passwords or tokens by
// design — the task is the moderation engine, not auth. The middleware loads
// the user's role and moderator category scopes on every request.
package auth

import (
	"context"
	"net/http"
	"strconv"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	db "communityvault/internal/db"
)

type ctxKey struct{}

// Principal is the authenticated identity attached to the request context.
type Principal struct {
	ID     int64
	Name   string
	Role   string
	Scopes map[string]bool // categories this moderator can moderate
}

func (p *Principal) IsAdmin() bool     { return p.Role == "admin" }
func (p *Principal) IsModerator() bool { return p.Role == "moderator" }

// CanModerate reports whether the principal may act in the given category.
func (p *Principal) CanModerate(category string) bool {
	if p.IsAdmin() {
		return true
	}
	return p.IsModerator() && p.Scopes[category]
}

// Middleware resolves X-User-ID into a Principal.
func Middleware(pool *pgxpool.Pool) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			raw := r.Header.Get("X-User-ID")
			if raw == "" {
				http.Error(w, `{"error":"missing X-User-ID header"}`, http.StatusUnauthorized)
				return
			}
			id, err := strconv.ParseInt(raw, 10, 64)
			if err != nil || id <= 0 {
				http.Error(w, `{"error":"invalid X-User-ID header"}`, http.StatusUnauthorized)
				return
			}
			q := db.New(pool)
			u, err := q.GetUser(r.Context(), id)
			if err == pgx.ErrNoRows {
				http.Error(w, `{"error":"unknown user"}`, http.StatusUnauthorized)
				return
			}
			if err != nil {
				http.Error(w, `{"error":"internal error"}`, http.StatusInternalServerError)
				return
			}
			scopes, err := q.ListScopes(r.Context(), id)
			if err != nil {
				http.Error(w, `{"error":"internal error"}`, http.StatusInternalServerError)
				return
			}
			p := &Principal{ID: u.ID, Name: u.Username, Role: u.Role, Scopes: map[string]bool{}}
			for _, s := range scopes {
				p.Scopes[s.Category] = true
			}
			next.ServeHTTP(w, r.WithContext(context.WithValue(r.Context(), ctxKey{}, p)))
		})
	}
}

// FromContext returns the Principal placed on the context by Middleware.
func FromContext(ctx context.Context) *Principal {
	p, _ := ctx.Value(ctxKey{}).(*Principal)
	return p
}
